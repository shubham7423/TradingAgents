from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 1


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


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        request_id=row["request_id"],
        request_hash=row["request_hash"],
        normalized_inputs=json.loads(row["normalized_inputs_json"]),
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
            if metadata_exists:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if row is not None and row["value"] != str(SCHEMA_VERSION):
                    raise IncompatibleState(
                        "INCOMPATIBLE_STATE: database schema "
                        f"{row['value']} is not supported; expected {SCHEMA_VERSION}"
                    )

            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    normalized_inputs_json TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS evidence (
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
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
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
                    run_id, request_id, request_hash, normalized_inputs_json, status, revision,
                    current_stage, frozen_config_json, instrument_json, lessons, state_schema,
                    prompt_schema, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    run_id,
                    request_id,
                    request_hash,
                    _json(normalized_inputs),
                    current_stage,
                    _json(frozen_config),
                    _json(instrument),
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
                INSERT OR IGNORE INTO evidence (
                    evidence_id, run_id, stage_id, tool_name, argument_hash, arguments_json,
                    status, fetched_at, requested_window_json, content, content_format,
                    source_json, warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    run_id,
                    expected_stage,
                    tool_name,
                    argument_hash,
                    _json(arguments),
                    status,
                    fetched_at,
                    _json(requested_window),
                    content,
                    content_format,
                    _json(source),
                    _json(warnings),
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
