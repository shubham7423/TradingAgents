from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

SCHEMA_VERSION = 2


class RequestIdConflict(RuntimeError):
    pass


class RunNotFound(RuntimeError):
    pass


class StaleRun(RuntimeError):
    pass


class IncompatibleState(RuntimeError):
    pass


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    request_id: str
    request_hash: str
    normalized_inputs: dict
    ticker: str
    status: str
    revision: int
    current_stage: str
    frozen_config: dict
    instrument: dict
    lessons: str
    state_schema: int
    prompt_schema: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    run_id: str
    stage_id: str
    tool_name: str
    argument_hash: str
    arguments: dict
    status: str
    fetched_at: str
    requested_window: dict
    content: str
    content_format: str
    source: dict
    warnings: list[str]


@dataclass(frozen=True)
class StageOutputRecord:
    receipt_id: str
    run_id: str
    stage_id: str
    role_key: str
    output_kind: Literal["text", "structured"]
    canonical_output: object
    rendered_output: str
    output_hash: str
    accepted_revision: int
    accepted_at: str


@dataclass(frozen=True)
class AnalysisSnapshot:
    run: RunRecord
    outputs: list[StageOutputRecord]
    evidence: list[EvidenceRecord]


@dataclass(frozen=True)
class EvidenceRequirement:
    tool_name: str
    arguments: dict[str, object]


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        request_id=row["request_id"],
        request_hash=row["request_hash"],
        normalized_inputs=json.loads(row["normalized_inputs_json"]),
        ticker=row["ticker"],
        status=row["status"],
        revision=row["revision"],
        current_stage=row["current_stage"],
        frozen_config=json.loads(row["frozen_config_json"]),
        instrument=json.loads(row["instrument_json"]),
        lessons=row["lessons"],
        state_schema=row["state_schema"],
        prompt_schema=row["prompt_schema"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _evidence_record(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=row["evidence_id"],
        run_id=row["run_id"],
        stage_id=row["stage_id"],
        tool_name=row["tool_name"],
        argument_hash=row["argument_hash"],
        arguments=json.loads(row["arguments_json"]),
        status=row["status"],
        fetched_at=row["fetched_at"],
        requested_window=json.loads(row["requested_window_json"]),
        content=row["content"],
        content_format=row["content_format"],
        source=json.loads(row["source_json"]),
        warnings=json.loads(row["warnings_json"]),
    )


def _stage_output_record(row: sqlite3.Row) -> StageOutputRecord:
    return StageOutputRecord(
        receipt_id=row["receipt_id"],
        run_id=row["run_id"],
        stage_id=row["stage_id"],
        role_key=row["role_key"],
        output_kind=row["output_kind"],
        canonical_output=json.loads(row["canonical_output_json"]),
        rendered_output=row["rendered_output"],
        output_hash=row["output_hash"],
        accepted_revision=row["accepted_revision"],
        accepted_at=row["accepted_at"],
    )


class PluginStore:
    def __init__(self, state_root: str | Path):
        root = Path(state_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        self._database_path = root / "plugin.sqlite3"
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            metadata_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            if not metadata_exists:
                connection.execute("PRAGMA journal_mode=WAL")
                self._create_schema(connection)
                return

            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            version = None if row is None else row["value"]
            if version not in {"1", str(SCHEMA_VERSION)}:
                raise IncompatibleState(
                    "INCOMPATIBLE_STATE: database schema "
                    f"{version} is not supported; expected {SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA journal_mode=WAL")
            if version == "1":
                self._migrate_v1(connection)

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                normalized_inputs_json TEXT NOT NULL,
                ticker TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('active','ready_to_finalize','completed','cancelled')
                ),
                revision INTEGER NOT NULL,
                current_stage TEXT NOT NULL,
                frozen_config_json TEXT NOT NULL,
                instrument_json TEXT NOT NULL,
                lessons TEXT NOT NULL,
                state_schema INTEGER NOT NULL,
                prompt_schema INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                stage_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                argument_hash TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('success','no_data','unavailable')),
                fetched_at TEXT NOT NULL,
                requested_window_json TEXT NOT NULL,
                content TEXT NOT NULL,
                content_format TEXT NOT NULL CHECK (content_format IN ('text','json')),
                source_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                UNIQUE (run_id, stage_id, tool_name, argument_hash)
            );
            CREATE TABLE stage_outputs (
                receipt_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                stage_id TEXT NOT NULL,
                role_key TEXT NOT NULL,
                output_kind TEXT NOT NULL CHECK (output_kind IN ('text','structured')),
                canonical_output_json TEXT NOT NULL,
                rendered_output TEXT NOT NULL,
                output_hash TEXT NOT NULL,
                accepted_revision INTEGER NOT NULL,
                accepted_at TEXT NOT NULL,
                UNIQUE (run_id, stage_id)
            );
            CREATE INDEX idx_stage_outputs_run_revision
            ON stage_outputs(run_id, accepted_revision);
            CREATE INDEX idx_runs_listing
            ON runs(status, ticker, created_at DESC, run_id DESC);
            """
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE runs ADD COLUMN ticker TEXT")
        rows = connection.execute("SELECT run_id, normalized_inputs_json FROM runs").fetchall()
        for row in rows:
            try:
                ticker = json.loads(row["normalized_inputs_json"])["ticker"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise IncompatibleState(
                    f"INCOMPATIBLE_STATE: run {row['run_id']} has no valid ticker"
                ) from exc
            if not isinstance(ticker, str):
                raise IncompatibleState(
                    f"INCOMPATIBLE_STATE: run {row['run_id']} has no valid ticker"
                )
            connection.execute("UPDATE runs SET ticker = ? WHERE run_id = ?", (ticker, row["run_id"]))
        connection.execute(
            """
            CREATE TABLE stage_outputs (
                receipt_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                stage_id TEXT NOT NULL,
                role_key TEXT NOT NULL,
                output_kind TEXT NOT NULL CHECK (output_kind IN ('text','structured')),
                canonical_output_json TEXT NOT NULL,
                rendered_output TEXT NOT NULL,
                output_hash TEXT NOT NULL,
                accepted_revision INTEGER NOT NULL,
                accepted_at TEXT NOT NULL,
                UNIQUE (run_id, stage_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_stage_outputs_run_revision "
            "ON stage_outputs(run_id, accepted_revision)"
        )
        connection.execute(
            "CREATE INDEX idx_runs_listing "
            "ON runs(status, ticker, created_at DESC, run_id DESC)"
        )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )

    def create_run(
        self,
        *,
        request_id: str,
        request_hash: str,
        normalized_inputs: dict,
        current_stage: str,
        frozen_config: dict,
        instrument: dict,
        lessons: str,
        now: str,
    ) -> tuple[RunRecord, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise RequestIdConflict(
                        f"REQUEST_ID_CONFLICT: request {request_id} has a different payload"
                    )
                return _run_record(row), False

            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, request_id, request_hash, normalized_inputs_json, ticker, status, revision,
                    current_stage, frozen_config_json, instrument_json, lessons, state_schema,
                    prompt_schema, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    run_id,
                    request_id,
                    request_hash,
                    canonical_json(normalized_inputs),
                    normalized_inputs["ticker"],
                    current_stage,
                    canonical_json(frozen_config),
                    canonical_json(instrument),
                    lessons,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return _run_record(row), True

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"RUN_NOT_FOUND: analysis {run_id} does not exist")
        return _run_record(row)

    def get_snapshot(self, run_id: str) -> AnalysisSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN")
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFound(f"RUN_NOT_FOUND: analysis {run_id} does not exist")
            outputs = connection.execute(
                "SELECT * FROM stage_outputs WHERE run_id = ? ORDER BY accepted_revision",
                (run_id,),
            ).fetchall()
            evidence = connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY fetched_at, evidence_id",
                (run_id,),
            ).fetchall()
        return AnalysisSnapshot(
            _run_record(run),
            [_stage_output_record(row) for row in outputs],
            [_evidence_record(row) for row in evidence],
        )

    def get_stage_output(self, run_id: str, stage_id: str) -> StageOutputRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM stage_outputs WHERE run_id = ? AND stage_id = ?",
                (run_id, stage_id),
            ).fetchone()
        return None if row is None else _stage_output_record(row)

    def get_evidence(self, run_id: str, evidence_id: str) -> EvidenceRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? AND evidence_id = ?",
                (run_id, evidence_id),
            ).fetchone()
        if row is None:
            raise RunNotFound(
                f"RUN_NOT_FOUND: evidence {evidence_id} does not exist in {run_id}"
            )
        return _evidence_record(row)

    def list_runs(
        self,
        ticker: str | None,
        status: str | None,
        limit: int,
        before: tuple[str, str] | None,
    ) -> list[RunRecord]:
        predicates = []
        parameters: list[object] = []
        if ticker is not None:
            predicates.append("ticker = ?")
            parameters.append(ticker)
        if status is not None:
            predicates.append("status = ?")
            parameters.append(status)
        if before is not None:
            predicates.append("(created_at < ? OR (created_at = ? AND run_id < ?))")
            parameters.extend((before[0], before[0], before[1]))
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs{where} ORDER BY created_at DESC, run_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [_run_record(row) for row in rows]

    def find_evidence(
        self, run_id: str, stage_id: str, tool_name: str, argument_hash: str
    ) -> EvidenceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM evidence
                WHERE run_id = ? AND stage_id = ? AND tool_name = ? AND argument_hash = ?
                """,
                (run_id, stage_id, tool_name, argument_hash),
            ).fetchone()
        return None if row is None else _evidence_record(row)

    def save_evidence(
        self,
        *,
        run_id: str,
        expected_stage: str,
        tool_name: str,
        argument_hash: str,
        arguments: dict,
        status: str,
        fetched_at: str,
        requested_window: dict,
        content: str,
        content_format: str,
        source: dict,
        warnings: list[str],
    ) -> tuple[EvidenceRecord, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status, current_stage FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RunNotFound(f"RUN_NOT_FOUND: analysis {run_id} does not exist")
            if run["status"] != "active" or run["current_stage"] != expected_stage:
                raise StaleRun(
                    f"STALE_RUN: analysis {run_id} is not active at stage {expected_stage}"
                )

            evidence_id = str(uuid4())
            inserted = connection.execute(
                """
                INSERT INTO evidence (
                    evidence_id, run_id, stage_id, tool_name, argument_hash, arguments_json,
                    status, fetched_at, requested_window_json, content, content_format,
                    source_json, warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, stage_id, tool_name, argument_hash) DO NOTHING
                """,
                (
                    evidence_id,
                    run_id,
                    expected_stage,
                    tool_name,
                    argument_hash,
                    canonical_json(arguments),
                    status,
                    fetched_at,
                    canonical_json(requested_window),
                    content,
                    content_format,
                    canonical_json(source),
                    canonical_json(warnings),
                ),
            ).rowcount
            row = connection.execute(
                """
                SELECT * FROM evidence
                WHERE run_id = ? AND stage_id = ? AND tool_name = ? AND argument_hash = ?
                """,
                (run_id, expected_stage, tool_name, argument_hash),
            ).fetchone()
            return _evidence_record(row), bool(inserted)

    def list_evidence(self, run_id: str) -> list[EvidenceRecord]:
        with self._connect() as connection:
            run = connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFound(f"RUN_NOT_FOUND: analysis {run_id} does not exist")
            rows = connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY fetched_at, evidence_id",
                (run_id,),
            ).fetchall()
        return [_evidence_record(row) for row in rows]
