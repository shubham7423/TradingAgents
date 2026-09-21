import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.plugin.store import (
    EvidenceRequirement,
    IncompatibleState,
    MissingEvidence,
    PluginStore,
    RequestIdConflict,
    RunCancelled,
    RunNotActive,
    RunNotFound,
    StageAlreadyAccepted,
    StaleRevision,
    StaleRun,
    WrongStage,
)


def run_values(request_hash="hash-one", now="2026-09-15T12:00:00+00:00"):
    return {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "request_hash": request_hash,
        "normalized_inputs": {"ticker": "AAPL"},
        "current_stage": "analyst/market",
        "frozen_config": {"data_vendors": {"core_stock_apis": "yfinance"}},
        "instrument": {"requested_symbol": "AAPL", "canonical_symbol": "AAPL"},
        "lessons": "past lesson",
        "now": now,
    }


def prepared_stage_values(run, **overrides):
    values = {
        "run_id": run.run_id,
        "stage_id": "analyst/market",
        "expected_revision": 1,
        "role_key": "market",
        "output_kind": "text",
        "canonical_output": "Market report",
        "rendered_output": "Market report",
        "output_hash": "output-hash",
        "required_evidence": (),
        "next_stage": "research/bull/1",
        "next_status": "active",
        "now": "2026-09-15T12:02:00+00:00",
    }
    values.update(overrides)
    return values


def _set_run_fields(store, run_id, **fields):
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            f"UPDATE runs SET {assignments} WHERE run_id = ?",
            (*fields.values(), run_id),
        )


def _save_market_evidence(store, run, status="success", arguments=None):
    return store.save_evidence(
        run_id=run.run_id,
        expected_stage="analyst/market",
        tool_name="get_verified_market_snapshot",
        argument_hash="market-args",
        arguments=arguments or {"symbol": "AAPL", "curr_date": "2026-09-15"},
        status=status,
        fetched_at="2026-09-15T12:01:00+00:00",
        requested_window={},
        content="market snapshot",
        content_format="text",
        source={"vendor": "yfinance"},
        warnings=[],
    )


def _create_v1_database(path):
    run_id = "11111111-1111-4111-8111-111111111111"
    evidence_id = "22222222-2222-4222-8222-222222222222"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL, normalized_inputs_json TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL, current_stage TEXT NOT NULL,
                frozen_config_json TEXT NOT NULL, instrument_json TEXT NOT NULL,
                lessons TEXT NOT NULL, state_schema INTEGER NOT NULL,
                prompt_schema INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                stage_id TEXT NOT NULL, tool_name TEXT NOT NULL, argument_hash TEXT NOT NULL,
                arguments_json TEXT NOT NULL, status TEXT NOT NULL, fetched_at TEXT NOT NULL,
                requested_window_json TEXT NOT NULL, content TEXT NOT NULL,
                content_format TEXT NOT NULL, source_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
                UNIQUE (run_id, stage_id, tool_name, argument_hash)
            );
            INSERT INTO metadata VALUES ('schema_version', '1');
            """
        )
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, 'hash', ?, 'active', 1, 'analyst/market', "
            "'{}', '{}', '', 1, 1, ?, ?)",
            (
                run_id,
                "33333333-3333-4333-8333-333333333333",
                '{"ticker":"AAPL"}',
                "2026-09-15T12:00:00+00:00",
                "2026-09-15T12:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, 'analyst/market', 'get_stock_data', 'args', "
            "'{}', 'success', ?, '{}', 'prices', 'text', '{}', '[]')",
            (evidence_id, run_id, "2026-09-15T12:01:00+00:00"),
        )
    return run_id, evidence_id


def test_create_run_is_idempotent_and_survives_restart(tmp_path):
    store = PluginStore(tmp_path)
    first, first_created = store.create_run(**run_values())
    repeated, repeated_created = store.create_run(**run_values())
    reopened = PluginStore(tmp_path).get_run(first.run_id)

    assert first_created is True
    assert repeated_created is False
    assert repeated.run_id == first.run_id
    assert reopened == first
    assert first.status == "active"
    assert first.revision == 1


def test_changed_request_payload_conflicts_without_second_run(tmp_path):
    store = PluginStore(tmp_path)
    store.create_run(**run_values())

    with pytest.raises(RequestIdConflict, match="REQUEST_ID_CONFLICT"):
        store.create_run(**run_values(request_hash="hash-two"))

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_evidence_is_unique_and_stale_stage_is_rejected(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    values = {
        "run_id": run.run_id,
        "expected_stage": "analyst/market",
        "tool_name": "get_stock_data",
        "argument_hash": "args-hash",
        "arguments": {
            "symbol": "AAPL",
            "start_date": "2026-09-01",
            "end_date": "2026-09-15",
        },
        "status": "success",
        "fetched_at": "2026-09-15T12:01:00+00:00",
        "requested_window": {"start": "2026-09-01", "end": "2026-09-15"},
        "content": "prices",
        "content_format": "text",
        "source": {"vendor": "yfinance"},
        "warnings": [],
    }

    first, created = store.save_evidence(**values)
    repeated, repeated_created = store.save_evidence(**values)

    assert created is True
    assert repeated_created is False
    assert repeated.evidence_id == first.evidence_id
    assert store.find_evidence(
        run.run_id, "analyst/market", "get_stock_data", "args-hash"
    ) == first
    with pytest.raises(StaleRun, match="STALE_RUN"):
        store.save_evidence(**{**values, "expected_stage": "analyst/news"})


def test_invalid_evidence_status_is_rejected_without_persistence(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        store.save_evidence(
            run_id=run.run_id,
            expected_stage="analyst/market",
            tool_name="get_stock_data",
            argument_hash="args-hash",
            arguments={"symbol": "AAPL"},
            status="retryable_error",
            fetched_at="2026-09-15T12:01:00+00:00",
            requested_window={"start": "2026-09-01", "end": "2026-09-15"},
            content="rate limited",
            content_format="text",
            source={"vendor": "yfinance"},
            warnings=[],
        )

    assert store.list_evidence(run.run_id) == []


def test_incompatible_schema_is_rejected_without_mutation(tmp_path):
    database = tmp_path / "plugin.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '999')")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    with pytest.raises(
        IncompatibleState,
        match="INCOMPATIBLE_STATE: database schema 999 is not supported; expected 2",
    ):
        PluginStore(tmp_path)

    with sqlite3.connect(database) as connection:
        value = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert (value, journal_mode) == ("999", "delete")


def test_v1_database_migrates_without_losing_run_or_evidence(tmp_path):
    database = tmp_path / "plugin.sqlite3"
    run_id, evidence_id = _create_v1_database(database)

    store = PluginStore(tmp_path)
    snapshot = store.get_snapshot(run_id)

    assert snapshot.run.ticker == "AAPL"
    assert snapshot.outputs == []
    assert [item.evidence_id for item in snapshot.evidence] == [evidence_id]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_snapshot_and_stage_output_reads_are_run_scoped(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())

    assert store.get_snapshot(run.run_id).run == run
    assert store.get_stage_output(run.run_id, "analyst/market") is None
    with pytest.raises(RunNotFound, match="RUN_NOT_FOUND"):
        store.get_evidence(run.run_id, "22222222-2222-4222-8222-222222222222")


def test_list_runs_filters_and_uses_stable_newest_first_boundary(tmp_path):
    store = PluginStore(tmp_path)
    first, _ = store.create_run(**run_values(now="2026-09-15T12:00:00+00:00"))
    second_values = run_values(now="2026-09-16T12:00:00+00:00") | {
        "request_id": "33333333-3333-4333-8333-333333333333",
        "normalized_inputs": {"ticker": "MSFT"},
        "instrument": {"requested_symbol": "MSFT", "canonical_symbol": "MSFT"},
    }
    second, _ = store.create_run(**second_values)

    assert [run.run_id for run in store.list_runs(None, None, 10, None)] == [
        second.run_id,
        first.run_id,
    ]
    assert [run.run_id for run in store.list_runs("AAPL", "active", 10, None)] == [
        first.run_id
    ]
    assert store.list_runs(None, None, 10, (second.created_at, second.run_id)) == [first]


def test_unknown_run_is_rejected(tmp_path):
    store = PluginStore(tmp_path)
    run_id = "22222222-2222-4222-8222-222222222222"
    message = f"RUN_NOT_FOUND: analysis {run_id} does not exist"

    with pytest.raises(RunNotFound) as get_error:
        store.get_run(run_id)
    with pytest.raises(RunNotFound) as list_error:
        store.list_evidence(run_id)

    assert str(get_error.value) == message
    assert str(list_error.value) == message


def test_accept_stage_is_atomic_and_identical_retry_returns_receipt(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    values = prepared_stage_values(run)

    advanced, receipt, created = store.accept_stage(**values)
    retried, repeated, repeated_created = store.accept_stage(**values)

    assert created is True and repeated_created is False
    assert repeated == receipt
    assert retried == advanced
    assert receipt.accepted_revision == 1
    assert advanced.current_stage == "research/bull/1"
    assert advanced.revision == 2


def test_accept_stage_rejects_conflicting_retry_without_mutation(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    advanced, receipt, _ = store.accept_stage(**prepared_stage_values(run))

    with pytest.raises(StageAlreadyAccepted, match="^STAGE_ALREADY_ACCEPTED:"):
        store.accept_stage(**prepared_stage_values(run, output_hash="different-hash"))

    assert store.get_run(run.run_id) == advanced
    assert store.get_stage_output(run.run_id, "analyst/market") == receipt


@pytest.mark.parametrize(
    ("overrides", "error", "code"),
    [
        ({"expected_revision": 2}, StaleRevision, "STALE_REVISION"),
        ({"stage_id": "analyst/news"}, WrongStage, "WRONG_STAGE"),
    ],
)
def test_accept_stage_rejects_stale_revision_and_wrong_stage(tmp_path, overrides, error, code):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())

    with pytest.raises(error, match=f"^{code}:"):
        store.accept_stage(**prepared_stage_values(run, **overrides))

    assert store.get_run(run.run_id) == run
    assert store.get_stage_output(run.run_id, overrides.get("stage_id", "analyst/market")) is None


def test_accept_stage_rejects_cancelled_run_before_stage_and_revision_checks(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    cancelled, _ = store.cancel_run(run.run_id, 1, "2026-09-15T12:01:00+00:00")

    with pytest.raises(RunCancelled, match="^RUN_CANCELLED:"):
        store.accept_stage(
            **prepared_stage_values(run, stage_id="analyst/news", expected_revision=999)
        )

    assert store.get_run(run.run_id) == cancelled


def test_accept_stage_rejects_missing_or_mismatched_evidence_without_mutation(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    requirement = EvidenceRequirement(
        "get_verified_market_snapshot",
        {"symbol": "AAPL", "curr_date": "2026-09-15"},
    )
    _save_market_evidence(
        store,
        run,
        arguments={"symbol": "AAPL", "curr_date": "2026-09-14", "extra": True},
    )

    with pytest.raises(MissingEvidence, match="^MISSING_EVIDENCE:.*get_verified_market_snapshot"):
        store.accept_stage(**prepared_stage_values(run, required_evidence=(requirement,)))

    assert store.get_run(run.run_id) == run
    assert store.get_stage_output(run.run_id, "analyst/market") is None


@pytest.mark.parametrize("status", ["success", "no_data", "unavailable"])
def test_accept_stage_accepts_each_persisted_terminal_evidence_status(tmp_path, status):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    _save_market_evidence(store, run, status=status)
    requirement = EvidenceRequirement(
        "get_verified_market_snapshot",
        {"symbol": "AAPL", "curr_date": "2026-09-15"},
    )

    advanced, _, created = store.accept_stage(
        **prepared_stage_values(run, required_evidence=(requirement,))
    )

    assert created is True
    assert advanced.revision == 2


@pytest.mark.parametrize(("field", "value"), [("state_schema", 2), ("prompt_schema", 2)])
def test_accept_stage_rejects_unknown_per_run_schema_without_mutation(tmp_path, field, value):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    _set_run_fields(store, run.run_id, **{field: value})

    with pytest.raises(IncompatibleState, match="^INCOMPATIBLE_STATE:"):
        store.accept_stage(**prepared_stage_values(run))

    assert store.get_run(run.run_id).revision == 1
    assert store.get_stage_output(run.run_id, "analyst/market") is None


@pytest.mark.parametrize("same_payload", [True, False])
def test_two_writer_accept_stage_advances_once(tmp_path, same_payload):
    first_store = PluginStore(tmp_path)
    second_store = PluginStore(tmp_path)
    run, _ = first_store.create_run(**run_values())
    first_values = prepared_stage_values(run)
    second_values = prepared_stage_values(
        run,
        output_hash="output-hash" if same_payload else "different-hash",
    )

    def accept(store, values):
        try:
            return store.accept_stage(**values)
        except (StageAlreadyAccepted, StaleRevision) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: accept(*item),
                [(first_store, first_values), (second_store, second_values)],
            )
        )

    successes = [result for result in results if isinstance(result, tuple)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert first_store.get_run(run.run_id).revision == 2
    assert len(first_store.get_snapshot(run.run_id).outputs) == 1
    if same_payload:
        assert len(successes) == 2
        assert sorted(result[2] for result in successes) == [False, True]
        assert successes[0][1] == successes[1][1]
    else:
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], StageAlreadyAccepted)


@pytest.mark.parametrize("initial_status", ["active", "ready_to_finalize"])
def test_cancel_run_is_atomic_idempotent_and_preserves_stage(tmp_path, initial_status):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    _set_run_fields(store, run.run_id, status=initial_status, current_stage="portfolio")

    cancelled, changed = store.cancel_run(
        run.run_id, 1, "2026-09-15T12:01:00+00:00"
    )
    repeated, repeated_changed = store.cancel_run(
        run.run_id, 999, "2026-09-15T12:02:00+00:00"
    )

    assert changed is True and repeated_changed is False
    assert repeated == cancelled
    assert cancelled.status == "cancelled"
    assert cancelled.revision == 2
    assert cancelled.current_stage == "portfolio"


def test_cancel_run_rejects_stale_revision_and_completed_run(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())

    with pytest.raises(StaleRevision, match="^STALE_REVISION:"):
        store.cancel_run(run.run_id, 2, "2026-09-15T12:01:00+00:00")
    _set_run_fields(store, run.run_id, status="completed")
    with pytest.raises(RunNotActive, match="^RUN_NOT_ACTIVE:"):
        store.cancel_run(run.run_id, 1, "2026-09-15T12:01:00+00:00")

    assert store.get_run(run.run_id).revision == 1


def test_cancel_run_preserves_evidence_and_outputs(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    evidence, _ = _save_market_evidence(store, run)
    advanced, receipt, _ = store.accept_stage(**prepared_stage_values(run))

    cancelled, _ = store.cancel_run(
        run.run_id, advanced.revision, "2026-09-15T12:03:00+00:00"
    )
    snapshot = store.get_snapshot(run.run_id)

    assert snapshot.run == cancelled
    assert snapshot.outputs == [receipt]
    assert snapshot.evidence == [evidence]
