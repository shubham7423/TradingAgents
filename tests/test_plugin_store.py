import sqlite3

import pytest

from tradingagents.plugin.store import (
    IncompatibleState,
    PluginStore,
    RequestIdConflict,
    RunNotFound,
    StaleRun,
)


def run_values(request_hash="hash-one"):
    return {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "request_hash": request_hash,
        "normalized_inputs": {"ticker": "AAPL"},
        "current_stage": "analyst/market",
        "frozen_config": {"data_vendors": {"core_stock_apis": "yfinance"}},
        "instrument": {"requested_symbol": "AAPL", "canonical_symbol": "AAPL"},
        "lessons": "past lesson",
        "now": "2026-09-15T12:00:00+00:00",
    }


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


def test_incompatible_schema_is_rejected_without_mutation(tmp_path):
    store = PluginStore(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")

    with pytest.raises(
        IncompatibleState,
        match="INCOMPATIBLE_STATE: database schema 999 is not supported; expected 1",
    ):
        PluginStore(tmp_path)

    with sqlite3.connect(store.database_path) as connection:
        value = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert value == "999"


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
