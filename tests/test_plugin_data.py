import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.interface import VendorRouteResult
from tradingagents.plugin import data
from tradingagents.plugin.store import (
    IncompatibleState,
    PluginStore,
    RequestIdConflict,
    RunNotFound,
    StaleRevision,
    canonical_json,
    digest_json,
)

REQUEST_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def store(tmp_path):
    return PluginStore(tmp_path)


@pytest.fixture
def tools(store):
    return data.PluginTools(store)


def _run_count(store: PluginStore) -> int:
    with sqlite3.connect(store.database_path) as connection:
        return connection.execute("SELECT count(*) FROM runs").fetchone()[0]


def _stub_identity_and_date(monkeypatch):
    monkeypatch.setattr(
        data, "resolve_instrument_identity", lambda ticker: {"company_name": "Apple"}
    )
    monkeypatch.setattr(data, "get_current_date", lambda: "2026-09-15")


def _create_evidence_run(
    store: PluginStore,
    *,
    request_id: str = REQUEST_ID,
    stage: str = "analyst/market",
    analysis_date: str = "2026-09-15",
    frozen_config: dict | None = None,
    instrument: dict | None = None,
):
    record, _ = store.create_run(
        request_id=request_id,
        request_hash=request_id,
        normalized_inputs={
            "ticker": "AAPL",
            "asset_type": "stock",
            "analysis_date": analysis_date,
            "analysts": [stage.removeprefix("analyst/")],
            "research_rounds": 1,
            "risk_rounds": 1,
            "output_language": "English",
        },
        current_stage=stage,
        frozen_config=(
            {"tool_vendors": {"get_stock_data": "yfinance"}}
            if frozen_config is None
            else frozen_config
        ),
        instrument=(
            {
                "canonical_symbol": "AAPL",
                "source": "yfinance",
                "context": "Apple Inc. (AAPL, NASDAQ, USD)",
            }
            if instrument is None
            else instrument
        ),
        lessons="",
        now="2026-09-15T12:00:00+00:00",
    )
    return record


def _create_completed_market_stage(store: PluginStore):
    run = _create_evidence_run(store)
    store.save_evidence(
        run_id=run.run_id,
        expected_stage="analyst/market",
        tool_name="get_verified_market_snapshot",
        argument_hash="market-snapshot",
        arguments={"symbol": "AAPL", "curr_date": "2026-09-15"},
        status="success",
        fetched_at="2026-09-15T12:01:00+00:00",
        requested_window={},
        content="market snapshot",
        content_format="text",
        source={"vendor": "yfinance"},
        warnings=[],
    )
    return run


def test_get_analysis_default_section_and_evidence_modes(tools, store):
    run = _create_completed_market_stage(store)
    submitted = tools.submit_stage(run.run_id, "analyst/market", 1, "Market report")
    evidence_id = store.list_evidence(run.run_id)[0].evidence_id

    default = tools.get_analysis(run.run_id)
    section = tools.get_analysis(run.run_id, section="analyst/market")
    evidence = tools.get_analysis(run.run_id, evidence_id=evidence_id)

    assert submitted.receipt.rendered_output == "Market report"
    assert default.active_role == "bull"
    assert default.instructions
    assert [item.stage_id for item in default.sections] == ["analyst/market"]
    assert section.selected_section.rendered_output == "Market report"
    assert section.instructions is None
    assert evidence.evidence_page.run_id == run.run_id
    assert evidence.evidence_page.reused is True
    assert evidence.instructions is None


@pytest.mark.parametrize("field", ["state_schema", "prompt_schema"])
@pytest.mark.parametrize("mode", ["default", "section", "evidence"])
def test_get_analysis_rejects_incompatible_run_in_every_mode(tools, store, field, mode):
    run = _create_completed_market_stage(store)
    tools.submit_stage(run.run_id, "analyst/market", 1, "Market report")
    evidence_id = store.list_evidence(run.run_id)[0].evidence_id
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(f"UPDATE runs SET {field} = 2 WHERE run_id = ?", (run.run_id,))
    kwargs = {
        "default": {},
        "section": {"section": "analyst/market"},
        "evidence": {"evidence_id": evidence_id},
    }[mode]

    with pytest.raises(IncompatibleState, match="^INCOMPATIBLE_STATE:"):
        tools.get_analysis(run.run_id, **kwargs)


def test_get_analysis_section_uses_only_the_loaded_snapshot(tools, store, monkeypatch):
    run = _create_completed_market_stage(store)
    submitted = tools.submit_stage(run.run_id, "analyst/market", 1, "Market report")
    snapshot = store.get_snapshot(run.run_id)
    monkeypatch.setattr(store, "get_snapshot", lambda run_id: snapshot)
    monkeypatch.setattr(
        store,
        "get_stage_output",
        lambda *args: pytest.fail("section read escaped the loaded snapshot"),
    )

    result = tools.get_analysis(run.run_id, section="analyst/market")

    assert result.revision == snapshot.run.revision
    assert result.selected_section == result.sections[0]
    assert result.selected_section.receipt_id == submitted.receipt.receipt_id


def test_get_analysis_evidence_uses_only_the_loaded_snapshot(tools, store, monkeypatch):
    run = _create_completed_market_stage(store)
    snapshot = store.get_snapshot(run.run_id)
    evidence = snapshot.evidence[0]
    monkeypatch.setattr(store, "get_snapshot", lambda run_id: snapshot)
    monkeypatch.setattr(
        store,
        "get_evidence",
        lambda *args: pytest.fail("evidence read escaped the loaded snapshot"),
    )

    result = tools.get_analysis(run.run_id, evidence_id=evidence.evidence_id)

    assert result.revision == snapshot.run.revision
    assert result.evidence_page.evidence_id == result.evidence[0].evidence_id
    assert result.evidence_page.content == evidence.content


def test_get_analysis_rejects_invalid_read_mode_combinations(tools, store):
    run = _create_completed_market_stage(store)
    evidence_id = store.list_evidence(run.run_id)[0].evidence_id

    with pytest.raises(ValueError, match="mutually exclusive"):
        tools.get_analysis(run.run_id, section="analyst/market", evidence_id=evidence_id)
    with pytest.raises(ValueError, match="cursor requires evidence_id"):
        tools.get_analysis(run.run_id, cursor="anything")
    with pytest.raises(ValueError, match="page_size requires evidence_id"):
        tools.get_analysis(run.run_id, page_size=100)
    with pytest.raises(RunNotFound, match="RUN_NOT_FOUND"):
        tools.get_analysis(run.run_id, section="analyst/market")


def test_get_analysis_rejects_cross_run_evidence_and_pages_saved_content(tools, store):
    first = _create_completed_market_stage(store)
    second = _create_evidence_run(
        store,
        request_id="22222222-2222-4222-8222-222222222222",
    )
    evidence_id = store.list_evidence(first.run_id)[0].evidence_id

    with pytest.raises(RunNotFound, match="RUN_NOT_FOUND"):
        tools.get_analysis(second.run_id, evidence_id=evidence_id)

    record = store.get_evidence(first.run_id, evidence_id)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE evidence SET content = ? WHERE evidence_id = ?",
            ("x" * 32_001, record.evidence_id),
        )
    first_page = tools.get_analysis(first.run_id, evidence_id=evidence_id)
    second_page = tools.get_analysis(
        first.run_id,
        evidence_id=evidence_id,
        cursor=first_page.evidence_page.page.next_cursor,
    )
    assert len(first_page.evidence_page.content) == 32_000
    assert second_page.evidence_page.content == "x"
    assert second_page.evidence_page.page.complete is True


@pytest.mark.parametrize("status", ["cancelled", "ready_to_finalize"])
def test_get_analysis_terminal_modes_do_not_build_prompts(tools, store, status):
    run = _create_evidence_run(store)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE runs SET status = ?, current_stage = 'finalize' WHERE run_id = ?",
            (status, run.run_id),
        )

    result = tools.get_analysis(run.run_id)

    assert result.active_role is None
    assert result.instructions is None
    assert result.required_evidence == []
    assert result.output_schema is None


def test_list_analyses_filters_and_paginates_without_duplicates(tools, store):
    first = _create_evidence_run(store)
    second = _create_evidence_run(
        store,
        request_id="22222222-2222-4222-8222-222222222222",
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE runs SET created_at = ?, updated_at = ? WHERE run_id = ?",
            ("2026-09-16T12:00:00+00:00", "2026-09-16T12:00:00+00:00", second.run_id),
        )

    page = tools.list_analyses(ticker=" aapl ", status="active", limit=1)
    following = tools.list_analyses(limit=1, cursor=page.next_cursor)

    assert [item.run_id for item in page.analyses] == [second.run_id]
    assert [item.run_id for item in following.analyses] == [first.run_id]
    assert page.analyses[0].ticker == "AAPL"
    assert following.next_cursor is None


@pytest.mark.parametrize("limit", [0, 101, True])
def test_list_analyses_rejects_invalid_limits(tools, limit):
    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        tools.list_analyses(limit=limit)


def test_list_analyses_rejects_invalid_status(tools):
    with pytest.raises(ValueError, match="invalid analysis status"):
        tools.list_analyses(status="invalid")


@pytest.mark.parametrize("cursor", ["bad", "e30=", "eyJjcmVhdGVkX2F0IjoxLCJydW5faWQiOiJ4In0="])
def test_list_analyses_rejects_malformed_cursors(tools, cursor):
    with pytest.raises(ValueError, match="invalid analysis cursor"):
        tools.list_analyses(cursor=cursor)


def test_submit_and_cancel_analysis_return_revisioned_receipts(tools, store):
    run = _create_completed_market_stage(store)

    submitted = tools.submit_stage(run.run_id, "analyst/market", 1, "Market report")
    cancelled = tools.cancel_analysis(run.run_id, submitted.revision)
    repeated = tools.cancel_analysis(run.run_id, 999)

    assert submitted.model_dump() == {
        "run_id": run.run_id,
        "status": "active",
        "revision": 2,
        "current_stage": "research/bull/1",
        "receipt": {
            "receipt_id": submitted.receipt.receipt_id,
            "stage_id": "analyst/market",
            "role_key": "market",
            "output_kind": "text",
            "canonical_output": "Market report",
            "rendered_output": "Market report",
            "accepted_revision": 1,
            "accepted_at": submitted.receipt.accepted_at,
        },
    }
    assert cancelled.status == "cancelled"
    assert cancelled.revision == 3
    assert cancelled.changed is True
    assert repeated == cancelled.model_copy(update={"changed": False})

    with pytest.raises(StaleRevision, match="STALE_REVISION"):
        tools.cancel_analysis(_create_evidence_run(
            store, request_id="33333333-3333-4333-8333-333333333333"
        ).run_id, 9)


def test_start_analysis_defaults_and_freezes_first_stage(tools, monkeypatch):
    _stub_identity_and_date(monkeypatch)

    result = tools.start_analysis(
        request_id=REQUEST_ID,
        ticker="aapl",
        asset_type="stock",
    )

    assert result.status == "active"
    assert result.revision == 1
    assert result.current_stage == "analyst/market"
    assert result.analysis_date == "2026-09-15"
    assert result.analysts == ["market", "social", "news", "fundamentals"]
    assert result.instrument["canonical_symbol"] == "AAPL"
    assert result.instrument["requested_symbol"] == "AAPL"
    assert result.instrument["source"] == "yfinance"
    assert "llm_provider" not in result.frozen_config


def test_start_analysis_is_idempotent_for_normalized_inputs(tools, store, monkeypatch):
    _stub_identity_and_date(monkeypatch)

    first = tools.start_analysis(request_id=REQUEST_ID.upper(), ticker=" aapl ")
    repeated = tools.start_analysis(request_id=REQUEST_ID, ticker="AAPL")

    assert repeated == first
    assert _run_count(store) == 1

    with pytest.raises(RequestIdConflict, match="REQUEST_ID_CONFLICT"):
        tools.start_analysis(request_id=REQUEST_ID, ticker="MSFT")
    assert _run_count(store) == 1


def test_omitted_date_retry_keeps_original_resolved_date(tools, monkeypatch):
    monkeypatch.setattr(data, "resolve_instrument_identity", lambda ticker: {})
    dates = iter(["2026-09-15", "2026-09-16"])
    monkeypatch.setattr(data, "get_current_date", lambda: next(dates))

    first = tools.start_analysis(request_id=REQUEST_ID, ticker="AAPL")
    repeated = tools.start_analysis(request_id=REQUEST_ID, ticker="AAPL")

    assert repeated.run_id == first.run_id
    assert repeated.analysis_date == "2026-09-15"


def test_get_analysis_uses_only_stored_fields(tools, store, monkeypatch):
    calls = []
    monkeypatch.setattr(
        data,
        "resolve_instrument_identity",
        lambda ticker: calls.append(ticker) or {"company_name": "Apple"},
    )
    monkeypatch.setattr(data, "get_current_date", lambda: "2026-09-15")
    started = tools.start_analysis(
        request_id=REQUEST_ID,
        ticker="AAPL",
        analysts=["news", "market"],
    )
    store.save_evidence(
        run_id=started.run_id,
        expected_stage="analyst/news",
        tool_name="get_news",
        argument_hash="hash",
        arguments={"ticker": "AAPL"},
        status="success",
        fetched_at="2026-09-15T12:00:00+00:00",
        requested_window={"end": "2026-09-15"},
        content="news",
        content_format="text",
        source={"vendor": "yfinance"},
        warnings=["fallback used"],
    )
    monkeypatch.setattr(
        data,
        "resolve_instrument_identity",
        lambda ticker: pytest.fail("identity resolver called by get_analysis"),
    )

    loaded = tools.get_analysis(started.run_id)

    assert calls == ["AAPL"]
    workflow_fields = {
        "evidence",
        "active_role",
        "instructions",
        "required_evidence",
        "output_schema",
    }
    assert loaded.model_dump(exclude=workflow_fields) == started.model_dump(
        exclude=workflow_fields
    )
    assert loaded.evidence == [
        data.EvidenceMetadata(
            evidence_id=loaded.evidence[0].evidence_id,
            stage_id="analyst/news",
            tool_name="get_news",
            status="success",
            fetched_at="2026-09-15T12:00:00+00:00",
            source={"vendor": "yfinance"},
            warnings=["fallback used"],
        )
    ]


def test_freezes_only_data_config_and_applies_canonical_overrides(store, monkeypatch):
    _stub_identity_and_date(monkeypatch)
    server_config = {
        "data_cache_dir": "/cache",
        "news_article_limit": 20,
        "global_news_article_limit": 10,
        "global_news_lookback_days": 7,
        "global_news_queries": ["rates"],
        "data_vendors": {"core_stock_apis": "yfinance", "news_data": "yfinance"},
        "tool_vendors": {},
        "llm_provider": "openai",
        "results_dir": "/results",
        "checkpoint_enabled": True,
        "memory_log_path": None,
    }
    tools = data.PluginTools(store, server_config)

    result = tools.start_analysis(
        request_id=REQUEST_ID,
        ticker="AAPL",
        research_rounds=2,
        risk_rounds=3,
        output_language=" French ",
        vendor_overrides={
            "categories": {"core_stock_apis": " yfinance, alpha_vantage "},
            "tools": {"get_news": "alpha_vantage"},
        },
    )

    assert result.output_language == "French"
    assert result.frozen_config == {
        "data_cache_dir": "/cache",
        "news_article_limit": 20,
        "global_news_article_limit": 10,
        "global_news_lookback_days": 7,
        "global_news_queries": ["rates"],
        "data_vendors": {
            "core_stock_apis": "yfinance,alpha_vantage",
            "news_data": "yfinance",
        },
        "tool_vendors": {"get_news": "alpha_vantage"},
        "output_language": "French",
        "max_debate_rounds": 2,
        "max_risk_discuss_rounds": 3,
    }


def test_start_analysis_reads_point_in_time_lessons(store, monkeypatch):
    _stub_identity_and_date(monkeypatch)
    calls = {}

    class FakeMemoryLog:
        def __init__(self, config):
            calls["config"] = config

        def get_past_context(self, ticker, as_of):
            calls["context"] = (ticker, as_of)
            return "past lesson"

    monkeypatch.setattr(data, "TradingMemoryLog", FakeMemoryLog)
    server_config = {"memory_log_path": "/memory.md"}

    result = data.PluginTools(store, server_config).start_analysis(
        request_id=REQUEST_ID,
        ticker="xauusd",
        analysis_date="2026-09-14",
    )

    assert result.instrument["canonical_symbol"] == "GC=F"
    assert result.lessons == "past lesson"
    assert calls == {
        "config": server_config,
        "context": ("GC=F", "2026-09-14"),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "not-a-uuid"},
        {"ticker": "../AAPL"},
        {"analysis_date": "2026-02-30"},
        {"asset_type": "bond"},
        {"analysts": []},
        {"analysts": ["market", "market"]},
        {"analysts": ["unknown"]},
        {"research_rounds": 0},
        {"risk_rounds": 11},
        {"output_language": "   "},
        {"vendor_overrides": {"categories": {"unknown": "yfinance"}}},
        {"vendor_overrides": {"categories": {"core_stock_apis": "fred"}}},
        {"vendor_overrides": {"tools": {"unknown": "yfinance"}}},
        {"vendor_overrides": {"tools": {"get_macro_indicators": "yfinance"}}},
        {"vendor_overrides": {"tools": {"get_news": "yfinance,,alpha_vantage"}}},
        {"vendor_overrides": {"tools": {"get_news": "yfinance,yfinance"}}},
        {"vendor_overrides": {"tools": {"get_news": "default,yfinance"}}},
    ],
    ids=[
        "request UUID",
        "ticker",
        "calendar date",
        "asset type",
        "empty analysts",
        "duplicate analysts",
        "unknown analyst",
        "research rounds",
        "risk rounds",
        "empty language",
        "unknown category",
        "unknown category vendor",
        "unknown vendor tool",
        "vendor unavailable for tool",
        "blank vendor",
        "duplicate vendor",
        "default mixed with vendor",
    ],
)
def test_invalid_start_request_does_not_persist(change, tools, store, monkeypatch):
    from tradingagents.llm_clients import factory

    monkeypatch.setattr(
        factory,
        "create_llm_client",
        lambda *args, **kwargs: pytest.fail("LLM client constructed"),
    )
    monkeypatch.setattr(
        data,
        "resolve_instrument_identity",
        lambda ticker: pytest.fail("identity network path called"),
    )
    values = {"request_id": REQUEST_ID, "ticker": "AAPL", **change}

    with pytest.raises((ValidationError, ValueError)):
        tools.start_analysis(**values)

    assert _run_count(store) == 0


def test_canonical_digest_ignores_mapping_order():
    left = {"ticker": "AAPL", "options": {"b": 2, "a": 1}}
    right = {"options": {"a": 1, "b": 2}, "ticker": "AAPL"}

    assert canonical_json(left) == canonical_json(right)
    assert digest_json(left) == digest_json(right)


def test_execute_reuses_terminal_evidence(tools, store):
    run = _create_evidence_run(store)
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return VendorRouteResult(
            content={"price": 100, "currency": "USD"},
            status="success",
            source="yfinance",
        )

    arguments = {"ticker": "AAPL", "curr_date": "2026-09-15"}
    first = tools._execute(
        "get_stock_data", arguments, {"end": "2026-09-15"}, fetch, run.run_id, None, None
    )
    repeated = tools._execute(
        "get_stock_data", arguments, {"end": "2026-09-15"}, fetch, run.run_id, None, None
    )

    assert first.reused is False
    assert repeated.reused is True
    assert calls == 1
    assert first.evidence_id == repeated.evidence_id
    assert first.fetched_at == repeated.fetched_at
    assert first.content == repeated.content == '{"currency":"USD","price":100}'
    assert first.content_format == repeated.content_format == "json"


def test_execute_does_not_persist_retryable_errors(tools, store):
    run = _create_evidence_run(store)
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return VendorRouteResult(
            content="network down",
            status="error",
            source=None,
            warnings=("yfinance failed",),
            retryable=True,
        )

    arguments = {"ticker": "AAPL"}
    first = tools._execute(
        "get_stock_data", arguments, {}, fetch, run.run_id, None, None
    )
    repeated = tools._execute(
        "get_stock_data", arguments, {}, fetch, run.run_id, None, None
    )

    assert calls == 2
    assert first.status == repeated.status == "error"
    assert first.retryable is repeated.retryable is True
    assert first.evidence_id is repeated.evidence_id is None
    assert store.list_evidence(run.run_id) == []


def test_execute_pages_saved_evidence_and_rejects_cursor_misuse(tools, store):
    run = _create_evidence_run(store)
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return VendorRouteResult(content="x" * 1_200, status="success", source="yfinance")

    arguments = {"ticker": "AAPL", "variant": "original"}
    first = tools._execute(
        "get_stock_data", arguments, {}, fetch, run.run_id, None, 500
    )
    second = tools._execute(
        "get_stock_data", arguments, {}, fetch, run.run_id, first.page.next_cursor, 500
    )
    third = tools._execute(
        "get_stock_data", arguments, {}, fetch, run.run_id, second.page.next_cursor, 500
    )

    assert [len(first.content), len(second.content), len(third.content)] == [500, 500, 200]
    assert first.page == data.EvidencePage(
        cursor=None, next_cursor=first.page.next_cursor, complete=False
    )
    assert second.page.cursor == first.page.next_cursor
    assert second.page.next_cursor is not None
    assert second.page.complete is False
    assert third.page.cursor == second.page.next_cursor
    assert third.page.next_cursor is None
    assert third.page.complete is True
    assert calls == 1

    with pytest.raises(ValueError, match="cursor"):
        tools._execute("get_stock_data", arguments, {}, fetch, run.run_id, "%%%", 500)
    with pytest.raises(ValueError, match="cursor"):
        data._decode_cursor(data._encode_cursor([], 0))
    with pytest.raises(ValueError, match="cursor"):
        tools._execute(
            "get_stock_data",
            {**arguments, "variant": "changed"},
            {},
            fetch,
            run.run_id,
            first.page.next_cursor,
            500,
        )


def test_run_permission_and_date_validation_precede_fetch(tools, store):
    run = _create_evidence_run(store)
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return VendorRouteResult(content="unused", status="success", source="test")

    invalid_calls = [
        ("get_news", {"ticker": "AAPL"}, {}),
        ("get_stock_data", {"ticker": "MSFT"}, {}),
        ("get_stock_data", {"ticker": "AAPL", "curr_date": "2026-09-14"}, {}),
        (
            "get_stock_data",
            {"ticker": "AAPL", "start_date": "2026-09-15", "end_date": "2026-09-14"},
            {"start": "2026-09-15", "end": "2026-09-14"},
        ),
        (
            "get_stock_data",
            {"ticker": "AAPL", "start_date": "2026-09-15", "end_date": "2026-09-16"},
            {"start": "2026-09-15", "end": "2026-09-16"},
        ),
    ]

    for tool_name, arguments, requested_window in invalid_calls:
        with pytest.raises(ValueError):
            tools._execute(
                tool_name, arguments, requested_window, fetch, run.run_id, None, None
            )

    assert calls == 0
    assert store.list_evidence(run.run_id) == []


def test_execute_restores_process_config_and_isolates_runs(tools, store):
    original = get_config()
    first_config = {"tool_vendors": {"get_stock_data": "yfinance"}, "run": "first"}
    second_config = {
        "tool_vendors": {"get_stock_data": "alpha_vantage"},
        "run": "second",
    }
    first = _create_evidence_run(store, frozen_config=first_config)
    second = _create_evidence_run(
        store,
        request_id="22222222-2222-4222-8222-222222222222",
        frozen_config=second_config,
    )
    active_fetch = Barrier(2)
    observed = []

    def failing_fetch():
        observed.append(get_config())
        active_fetch.wait()
        active_fetch.wait()
        raise RuntimeError("fetch failed")

    def successful_fetch():
        observed.append(get_config())
        return VendorRouteResult(content="ok", status="success", source="test")

    with ThreadPoolExecutor(max_workers=2) as executor:
        failed = executor.submit(
            tools._execute,
            "get_stock_data",
            {"ticker": "AAPL"},
            {},
            failing_fetch,
            first.run_id,
            None,
            None,
        )
        active_fetch.wait()
        succeeded = executor.submit(
            tools._execute,
            "get_stock_data",
            {"ticker": "AAPL"},
            {},
            successful_fetch,
            second.run_id,
            None,
            None,
        )
        active_fetch.wait()

        with pytest.raises(RuntimeError, match="fetch failed"):
            failed.result()
        assert succeeded.result().content == "ok"

    assert observed == [first_config, second_config]
    assert get_config() == original


def test_execute_returns_frozen_identity_without_fetch_or_evidence(tools, store):
    run = _create_evidence_run(store)

    result = tools._execute(
        "resolve_instrument_identity",
        {"ticker": "AAPL"},
        {},
        lambda: pytest.fail("identity fetch called"),
        run.run_id,
        None,
        None,
    )

    assert data.json.loads(result.content) == run.instrument
    assert result.content_format == "json"
    assert result.source == "yfinance"
    assert result.reused is True
    assert result.evidence_id is None
    assert store.list_evidence(run.run_id) == []


def test_standalone_execution_rejects_paging(tools):
    def fetch():
        return VendorRouteResult(content="ok", status="success", source="test")

    with pytest.raises(ValueError, match="standalone"):
        tools._execute("get_news", {}, {}, fetch, None, "cursor", None)
    with pytest.raises(ValueError, match="standalone"):
        tools._execute("get_news", {}, {}, fetch, None, None, 500)


@pytest.mark.parametrize(
    ("content", "traced_status", "expected_status", "retryable"),
    [
        ("NO_DATA_AVAILABLE: none", "success", "no_data", False),
        ("No news found for AAPL", "success", "no_data", False),
        ("No global news found for 2026-09-15", "success", "no_data", False),
        ("No open prediction markets matched 'rates'", "success", "no_data", False),
        ("<no Reddit posts found mentioning AAPL across r/stocks>", "success", "no_data", False),
        (
            "<no StockTwits messages for $AAPL within 2020-01-01..2020-01-02 "
            "(public stream serves only recent messages)>",
            "success",
            "unavailable",
            False,
        ),
        ("Error fetching news", "success", "error", True),
        ("Service currently unavailable (network error: timeout)", "success", "error", True),
        ("<stocktwits unavailable: TimeoutError>", "success", "error", True),
        (
            "<Reddit unavailable: every source failed to fetch (r/stocks)>",
            "success",
            "error",
            True,
        ),
    ],
)
def test_execute_classifies_fixed_source_sentinels(
    tools, content, traced_status, expected_status, retryable
):
    result = tools._execute(
        "get_news",
        {},
        {},
        lambda: VendorRouteResult(
            content=content, status=traced_status, source="fixed-source"
        ),
        None,
        None,
        None,
    )

    assert result.content == content
    assert result.status == expected_status
    assert result.retryable is retryable


def test_execute_keeps_partial_reddit_success_with_warning(tools):
    content = (
        "<no Reddit posts found mentioning AAPL across r/stocks in the past 7 days>\n"
        "<unavailable (fetch failed): r/investing>"
    )

    result = tools._execute(
        "fetch_reddit_posts",
        {},
        {},
        lambda: VendorRouteResult(content=content, status="success", source="reddit"),
        None,
        None,
        None,
    )

    assert result.status == "success"
    assert result.retryable is False
    assert result.warnings == ["Reddit sources unavailable: r/investing"]


def test_execute_preserves_missing_key_unavailable(tools):
    result = tools._execute(
        "get_macro_indicators",
        {},
        {},
        lambda: VendorRouteResult(
            content=None,
            status="unavailable",
            source=None,
            warnings=("fred: VendorNotConfiguredError: missing API key",),
        ),
        None,
        None,
        None,
    )

    assert result.status == "unavailable"
    assert result.content == ""
    assert result.source == "unknown"
    assert result.retryable is False


@pytest.mark.parametrize(
    ("method_name", "domain_args", "routed_args", "expected_source"),
    [
        (
            "get_stock_data",
            ("AAPL", "2026-09-01", "2026-09-15"),
            ("get_stock_data", "AAPL", "2026-09-01", "2026-09-15"),
            "yfinance",
        ),
        (
            "get_indicators",
            ("AAPL", "rsi", "2026-09-15", 14),
            ("get_indicators", "AAPL", "rsi", "2026-09-15", 14),
            "yfinance",
        ),
        (
            "get_fundamentals",
            ("AAPL", "2026-09-15"),
            ("get_fundamentals", "AAPL", "2026-09-15"),
            "alpha_vantage",
        ),
        (
            "get_balance_sheet",
            ("AAPL", "annual", "2026-09-15"),
            ("get_balance_sheet", "AAPL", "annual", "2026-09-15"),
            "alpha_vantage",
        ),
        (
            "get_cashflow",
            ("AAPL", "annual", "2026-09-15"),
            ("get_cashflow", "AAPL", "annual", "2026-09-15"),
            "alpha_vantage",
        ),
        (
            "get_income_statement",
            ("AAPL", "annual", "2026-09-15"),
            ("get_income_statement", "AAPL", "annual", "2026-09-15"),
            "alpha_vantage",
        ),
    ],
)
def test_market_and_fundamental_methods_forward_exact_arguments(
    tools, monkeypatch, method_name, domain_args, routed_args, expected_source
):
    calls = []

    def route(*args):
        calls.append(args)
        return VendorRouteResult(content={"method": method_name}, status="success", source=expected_source)

    monkeypatch.setattr(data, "route_to_vendor_traced", route)

    result = getattr(tools, method_name)(*domain_args)

    assert calls == [routed_args]
    assert result.status == "success"
    assert result.content == f'{{"method":"{method_name}"}}'
    assert result.content_format == "json"
    assert result.source == expected_source
    assert result.warnings == []
    assert result.reused is False
    assert result.retryable is False
    assert result.evidence_id is None
    assert result.run_id is None
    assert result.page.complete is True


def test_market_snapshot_uses_verified_builder_and_unknown_source(tools, monkeypatch):
    calls = []
    monkeypatch.setattr(
        data,
        "build_verified_market_snapshot",
        lambda *args: calls.append(args) or "verified snapshot",
    )

    result = tools.get_verified_market_snapshot("AAPL", "2026-09-15", 12)

    assert calls == [("AAPL", "2026-09-15", 12)]
    assert result.status == "success"
    assert result.content == "verified snapshot"
    assert result.source == "unknown"


def test_market_snapshot_maps_no_data_and_retryable_errors(tools, monkeypatch):
    error = data.NoMarketDataError("AAPL", detail="no rows")
    monkeypatch.setattr(
        data,
        "build_verified_market_snapshot",
        lambda *args: (_ for _ in ()).throw(error),
    )

    no_data = tools.get_verified_market_snapshot("AAPL", "2026-09-15")

    assert no_data.status == "no_data"
    assert no_data.content == (
        "NO_DATA_AVAILABLE: No usable market data for 'AAPL' from any configured vendor "
        "(no rows). The symbol may be invalid, delisted, not covered, or the vendor returned "
        "stale data. Do not estimate or fabricate values — report that data is unavailable "
        "for this symbol."
    )
    assert no_data.source == "unknown"
    assert no_data.retryable is False

    monkeypatch.setattr(
        data,
        "build_verified_market_snapshot",
        lambda *args: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    failed = tools.get_verified_market_snapshot("AAPL", "2026-09-15")

    assert failed.status == "error"
    assert failed.retryable is True
    assert "network down" in failed.content


def test_market_route_preserves_no_data_content(tools, monkeypatch):
    from tradingagents.dataflows import interface

    def no_rows(*args):
        raise data.NoMarketDataError(args[0], detail="no rows")

    monkeypatch.setattr(interface, "get_vendor", lambda *args: "yfinance")
    monkeypatch.setitem(interface.VENDOR_METHODS, "get_stock_data", {"yfinance": no_rows})

    result = tools.get_stock_data("AAPL", "2026-09-01", "2026-09-15")

    assert result.status == "no_data"
    assert result.content.startswith("NO_DATA_AVAILABLE: No usable market data for 'AAPL'")
    assert result.source == "unknown"


@pytest.mark.parametrize(
    ("method_name", "domain_args", "routed_args", "expected_source"),
    [
        (
            "get_news",
            ("AAPL", "2026-09-01", "2026-09-15"),
            ("get_news", "AAPL", "2026-09-01", "2026-09-15"),
            "yfinance",
        ),
        (
            "get_global_news",
            ("2026-09-15", 7, 20),
            ("get_global_news", "2026-09-15", 7, 20),
            "alpha_vantage",
        ),
        (
            "get_insider_transactions",
            ("AAPL",),
            ("get_insider_transactions", "AAPL"),
            "yfinance",
        ),
        (
            "get_macro_indicators",
            ("cpi", "2026-09-15", 365),
            ("get_macro_indicators", "cpi", "2026-09-15", 365),
            "fred",
        ),
        (
            "get_prediction_markets",
            ("rates", 4),
            ("get_prediction_markets", "rates", 4),
            "polymarket",
        ),
    ],
)
def test_contextual_methods_forward_exact_arguments(
    tools, monkeypatch, method_name, domain_args, routed_args, expected_source
):
    calls = []

    def route(*args):
        calls.append(args)
        return VendorRouteResult(content="evidence", status="success", source=expected_source)

    monkeypatch.setattr(data, "route_to_vendor_traced", route)

    result = getattr(tools, method_name)(*domain_args)

    assert calls == [routed_args]
    assert result.status == "success"
    assert result.content == "evidence"
    assert result.source == expected_source


def test_contextual_missing_fred_is_terminal_unavailable(tools, monkeypatch):
    monkeypatch.setattr(
        data,
        "route_to_vendor_traced",
        lambda *args: VendorRouteResult(
            content="DATA_UNAVAILABLE: missing FRED API key",
            status="unavailable",
            source=None,
            warnings=("fred: VendorNotConfiguredError: missing API key",),
        ),
    )

    result = tools.get_macro_indicators("cpi", "2026-09-15")

    assert result.status == "unavailable"
    assert result.source == "unknown"
    assert result.retryable is False
    assert result.warnings == ["fred: VendorNotConfiguredError: missing API key"]


@pytest.mark.parametrize("method_name", ["get_insider_transactions", "get_prediction_markets"])
def test_contextual_historical_no_date_methods_warn(
    tools, store, monkeypatch, method_name
):
    run = _create_evidence_run(
        store,
        stage="analyst/news",
        analysis_date="2026-09-14",
    )
    monkeypatch.setattr(data, "get_current_date", lambda: "2026-09-15")
    monkeypatch.setattr(
        data,
        "route_to_vendor_traced",
        lambda *args: VendorRouteResult(content="live evidence", status="success", source="test"),
    )

    result = (
        tools.get_insider_transactions("AAPL", run_id=run.run_id)
        if method_name == "get_insider_transactions"
        else tools.get_prediction_markets("rates", run_id=run.run_id)
    )

    assert result.status == "success"
    assert any("historical" in warning and "point-in-time" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("method_name", "patched_name", "domain_args", "expected_call", "expected_source"),
    [
        (
            "fetch_stocktwits_messages",
            "_fetch_stocktwits_messages",
            ("AAPL", 20, "2026-09-01", "2026-09-15"),
            (("AAPL", 20), {"start_date": "2026-09-01", "end_date": "2026-09-15"}),
            "stocktwits",
        ),
        (
            "fetch_reddit_posts",
            "_fetch_reddit_posts",
            ("AAPL", 8, "2026-09-01", "2026-09-15"),
            (
                ("AAPL",),
                {
                    "limit_per_sub": 8,
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-15",
                },
            ),
            "reddit",
        ),
    ],
)
def test_social_methods_forward_bounded_windows(
    tools,
    monkeypatch,
    method_name,
    patched_name,
    domain_args,
    expected_call,
    expected_source,
):
    calls = []

    def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return "social evidence"

    monkeypatch.setattr(data, patched_name, fetch)

    result = getattr(tools, method_name)(*domain_args)

    assert calls == [expected_call]
    assert result.status == "success"
    assert result.source == expected_source


def test_social_failure_and_partial_reddit_classification(tools, monkeypatch):
    monkeypatch.setattr(
        data,
        "_fetch_stocktwits_messages",
        lambda *args, **kwargs: "<stocktwits unavailable: TimeoutError>",
    )
    stocktwits = tools.fetch_stocktwits_messages("AAPL")

    assert stocktwits.status == "error"
    assert stocktwits.retryable is True

    monkeypatch.setattr(
        data,
        "_fetch_reddit_posts",
        lambda *args, **kwargs: (
            "<no Reddit posts found mentioning AAPL across r/stocks in the past 7 days>\n"
            "<unavailable (fetch failed): r/investing>"
        ),
    )
    partial = tools.fetch_reddit_posts("AAPL")

    assert partial.status == "success"
    assert partial.retryable is False
    assert partial.warnings == ["Reddit sources unavailable: r/investing"]

    monkeypatch.setattr(
        data,
        "_fetch_reddit_posts",
        lambda *args, **kwargs: (
            "<Reddit unavailable: every source failed to fetch (r/stocks, r/investing)>"
        ),
    )
    failed = tools.fetch_reddit_posts("AAPL")

    assert failed.status == "error"
    assert failed.retryable is True


@pytest.mark.parametrize(
    ("ticker", "canonical", "asset_type", "context_word"),
    [
        ("CNC.TO", "CNC.TO", "stock", "instrument"),
        ("XAUUSD+", "GC=F", "stock", "instrument"),
        ("BTCUSD", "BTC-USD", "crypto", "asset"),
    ],
)
def test_identity_normalizes_and_builds_standalone_context(
    tools, monkeypatch, ticker, canonical, asset_type, context_word
):
    monkeypatch.setattr(
        data,
        "resolve_instrument_identity_data",
        lambda value: {"company_name": f"Identity for {value}"},
    )

    result = tools.resolve_instrument_identity(ticker)
    payload = data.json.loads(result.content)

    assert payload["requested_symbol"] == ticker.upper()
    assert payload["canonical_symbol"] == canonical
    assert payload["asset_type"] == asset_type
    assert f"The {context_word} to analyze is `{canonical}`" in payload["context"]
    assert result.source == "yfinance"
    assert result.warnings == []
    assert result.reused is False
    assert result.run_id is None


def test_identity_metadata_failure_keeps_normalized_result_with_warning(tools, monkeypatch):
    monkeypatch.setattr(data, "resolve_instrument_identity_data", lambda ticker: {})

    result = tools.resolve_instrument_identity("XAUUSD+")

    assert data.json.loads(result.content)["canonical_symbol"] == "GC=F"
    assert result.status == "success"
    assert result.source == "symbol_utils"
    assert result.warnings == ["yfinance identity metadata unavailable"]


def test_identity_run_returns_frozen_instrument_without_resolution(tools, store, monkeypatch):
    run = _create_evidence_run(store)
    monkeypatch.setattr(
        data,
        "resolve_instrument_identity_data",
        lambda ticker: pytest.fail("identity resolver called"),
    )

    result = tools.resolve_instrument_identity("AAPL", run_id=run.run_id)

    assert data.json.loads(result.content) == run.instrument
    assert result.source == "yfinance"
    assert result.reused is True


def test_identity_run_warns_when_frozen_metadata_is_unavailable(tools, store):
    run = _create_evidence_run(
        store,
        instrument={"canonical_symbol": "AAPL", "source": "symbol_utils"},
    )

    result = tools.resolve_instrument_identity("AAPL", run_id=run.run_id)

    assert result.source == "symbol_utils"
    assert result.warnings == ["yfinance identity metadata unavailable"]


def test_public_method_validation_precedes_fetch(tools, monkeypatch):
    from tradingagents.llm_clients import factory

    monkeypatch.setattr(
        factory,
        "create_llm_client",
        lambda *args, **kwargs: pytest.fail("LLM client constructed"),
    )
    monkeypatch.setattr(
        data,
        "route_to_vendor_traced",
        lambda *args: pytest.fail("route called"),
    )
    monkeypatch.setattr(
        data,
        "_fetch_stocktwits_messages",
        lambda *args, **kwargs: pytest.fail("StockTwits called"),
    )

    with pytest.raises(ValueError, match="one indicator"):
        tools.get_indicators("AAPL", "rsi,macd", "2026-09-15")
    with pytest.raises(ValueError, match="start_date"):
        tools.get_news("AAPL", "2026-09-16", "2026-09-15")
    with pytest.raises(ValueError, match="limit"):
        tools.fetch_stocktwits_messages("AAPL", 101)


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("get_stock_data", ("AAPL", None, None)),
        ("get_news", ("AAPL", None, None)),
    ],
)
def test_required_date_windows_reject_missing_dates_before_fetch(
    tools, monkeypatch, method_name, args
):
    calls = []
    monkeypatch.setattr(
        data,
        "route_to_vendor_traced",
        lambda *route_args: calls.append(route_args),
    )

    with pytest.raises(ValueError, match="start_date and end_date are required"):
        getattr(tools, method_name)(*args)

    assert calls == []


def test_status_reuse_and_continuation_do_not_construct_llm_or_call_network(
    tools, store, monkeypatch
):
    from tradingagents.llm_clients import factory

    run = _create_evidence_run(store)
    arguments = {
        "symbol": "AAPL",
        "start_date": "2026-09-01",
        "end_date": "2026-09-15",
    }
    store.save_evidence(
        run_id=run.run_id,
        expected_stage=run.current_stage,
        tool_name="get_stock_data",
        argument_hash=digest_json(arguments),
        arguments=arguments,
        status="success",
        fetched_at="2026-09-15T12:01:00+00:00",
        requested_window={"start": "2026-09-01", "end": "2026-09-15"},
        content="x" * 1_200,
        content_format="text",
        source={"vendor": "yfinance"},
        warnings=[],
    )
    monkeypatch.setattr(
        factory,
        "create_llm_client",
        lambda *args, **kwargs: pytest.fail("LLM client constructed"),
    )
    monkeypatch.setattr(
        data,
        "route_to_vendor_traced",
        lambda *args: pytest.fail("network route called"),
    )
    monkeypatch.setattr(
        data,
        "build_verified_market_snapshot",
        lambda *args: pytest.fail("snapshot network path called"),
    )
    monkeypatch.setattr(
        data,
        "_fetch_stocktwits_messages",
        lambda *args, **kwargs: pytest.fail("StockTwits called"),
    )
    monkeypatch.setattr(
        data,
        "_fetch_reddit_posts",
        lambda *args, **kwargs: pytest.fail("Reddit called"),
    )
    monkeypatch.setattr(
        data,
        "resolve_instrument_identity_data",
        lambda *args: pytest.fail("identity network path called"),
    )

    analysis = tools.get_analysis(run.run_id)
    first = tools.get_stock_data(
        "AAPL", "2026-09-01", "2026-09-15", run_id=run.run_id, page_size=500
    )
    second = tools.get_stock_data(
        "AAPL",
        "2026-09-01",
        "2026-09-15",
        run_id=run.run_id,
        cursor=first.page.next_cursor,
        page_size=500,
    )

    assert analysis.run_id == run.run_id
    assert first.reused is True
    assert second.reused is True
    assert [len(first.content), len(second.content)] == [500, 500]


def test_fallback_diagnostics_are_safe_when_saved_and_reused(tools, store, monkeypatch):
    from tradingagents.dataflows import interface

    run = _create_evidence_run(store, frozen_config={
        "tool_vendors": {"get_stock_data": "alpha_vantage,yfinance"},
    })
    calls = []

    def fail(*args):
        calls.append(args)
        raise RuntimeError("https://www.alphavantage.co/query?apikey=secret-token")

    monkeypatch.setitem(interface.VENDOR_METHODS, "get_stock_data", {
        "alpha_vantage": fail, "yfinance": lambda *args: "prices",
    })
    first = tools.get_stock_data("AAPL", "2026-09-01", "2026-09-15", run_id=run.run_id)
    repeated = tools.get_stock_data("AAPL", "2026-09-01", "2026-09-15", run_id=run.run_id)
    saved = PluginStore(store.database_path.parent).list_evidence(run.run_id)

    assert first.status == "success" and repeated.reused
    assert first.evidence_id == repeated.evidence_id
    assert len(calls) == 1
    assert len(saved) == 1
    assert saved[0].warnings == first.warnings == repeated.warnings
    assert "secret-token" not in first.model_dump_json() + str(saved)
    assert "https://" not in first.model_dump_json() + str(saved)


@pytest.mark.parametrize("method_name", [
    "get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement",
    "get_insider_transactions",
])
def test_provider_retrieval_failures_are_never_evidence(tools, store, monkeypatch, method_name):
    from tradingagents.dataflows import date_window, y_finance

    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-09-15")
    stage = "analyst/news" if method_name == "get_insider_transactions" else "analyst/fundamentals"
    run = _create_evidence_run(store, stage=stage, frozen_config={
        "tool_vendors": {method_name: "yfinance"},
    })
    calls = []

    def fail(ticker):
        calls.append(ticker)
        raise RuntimeError("temporary provider failure")

    monkeypatch.setattr(y_finance.yf, "Ticker", fail)
    kwargs = {} if method_name == "get_insider_transactions" else {"curr_date": "2026-09-15"}
    for _ in range(2):
        result = getattr(tools, method_name)("AAPL", run_id=run.run_id, **kwargs)
        assert result.status == "error" and result.retryable
        assert result.content.startswith("Error retrieving")
        assert result.evidence_id is None and not result.reused
    assert calls == ["AAPL", "AAPL"]
    assert store.list_evidence(run.run_id) == []


@pytest.mark.parametrize("response", [
    RuntimeError("temporary provider failure"),
    "",
    "date,RSI\n2026-09-15,50",
    "time,close\n2026-09-15,50",
])
def test_indicator_failure_sentinels_are_never_evidence(tools, store, monkeypatch, response):
    from tradingagents.dataflows import alpha_vantage_indicator

    run = _create_evidence_run(store, frozen_config={
        "tool_vendors": {"get_indicators": "alpha_vantage"},
    })
    calls = []

    def request(*args):
        calls.append(args)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(alpha_vantage_indicator, "_make_api_request", request)
    for _ in range(2):
        result = tools.get_indicators("AAPL", "rsi", "2026-09-15", run_id=run.run_id)
        assert result.status == "error" and result.retryable
        assert result.evidence_id is None and not result.reused
    assert len(calls) == 2
    assert store.list_evidence(run.run_id) == []


@pytest.mark.parametrize("missing", [False, True])
def test_alpha_vantage_auth_failures_have_distinct_persistence(tools, store, monkeypatch, missing):
    from types import SimpleNamespace

    from tradingagents.dataflows import alpha_vantage_common as av, interface

    run = _create_evidence_run(store, stage="analyst/fundamentals", frozen_config={
        "tool_vendors": {"get_balance_sheet": "alpha_vantage"},
    })
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            text='{"Information":"The parameter apikey is invalid or missing."}',
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(av.requests, "get", request)
    if missing:
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "rejected-key")
    results = [tools.get_balance_sheet("AAPL", curr_date="2026-09-15", run_id=run.run_id)
               for _ in range(2)]

    assert [result.status for result in results] == ["unavailable" if missing else "error"] * 2
    assert [result.retryable for result in results] == [not missing] * 2
    assert results[1].reused is missing
    assert len(store.list_evidence(run.run_id)) == (1 if missing else 0)
    assert len(calls) == (0 if missing else 2)
    if not missing:
        assert all(result.evidence_id is None for result in results)

    with pytest.raises(av.AlphaVantageNotConfiguredError) as raised, monkeypatch.context() as patch:
        patch.setattr(interface, "get_vendor", lambda *args: "alpha_vantage")
        interface.route_to_vendor("get_balance_sheet", "AAPL", "quarterly", "2026-09-15")
    assert type(raised.value) is av.AlphaVantageNotConfiguredError


@pytest.mark.parametrize("method_name", ["fetch_stocktwits_messages", "fetch_reddit_posts"])
def test_run_social_requires_explicit_window_before_fetch(tools, store, monkeypatch, method_name):
    run = _create_evidence_run(store, stage="analyst/social", analysis_date="2020-01-02")
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return "social evidence"

    monkeypatch.setattr(data, f"_{method_name}", fetch)
    method = getattr(tools, method_name)
    for window in ({}, {"start_date": "2020-01-01"}, {"end_date": "2020-01-02"},
                   {"start_date": "2020-01-01", "end_date": "2020-01-03"}):
        with pytest.raises(ValueError, match="start_date|end_date"):
            method("AAPL", run_id=run.run_id, **window)
    assert calls == []
    assert store.list_evidence(run.run_id) == []

    bounded = method("AAPL", run_id=run.run_id, start_date="2020-01-01", end_date="2020-01-02")
    standalone = method("AAPL")
    assert bounded.status == standalone.status == "success"
    assert calls[0]["end_date"] == "2020-01-02"
    assert calls[1]["start_date"] is calls[1]["end_date"] is None


@pytest.mark.parametrize("field", ["Error Message", "Information", "Note"])
def test_alpha_vantage_error_payloads_are_not_evidence(tools, store, monkeypatch, field):
    import json
    from types import SimpleNamespace

    from tradingagents.dataflows import alpha_vantage_common as av

    run = _create_evidence_run(store, stage="analyst/fundamentals", frozen_config={
        "tool_vendors": {"get_balance_sheet": "alpha_vantage"},
    })
    body = json.dumps({field: "Provider could not process this request."})
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(text=body, raise_for_status=lambda: None)

    monkeypatch.setattr(av.requests, "get", request)
    for _ in range(2):
        result = tools.get_balance_sheet("AAPL", curr_date="2026-09-15", run_id=run.run_id)
        assert result.status == "error" and result.retryable
        assert result.content == body
        assert result.evidence_id is None
    assert len(calls) == 2
    assert store.list_evidence(run.run_id) == []


@pytest.mark.parametrize("path", ["indicator", "snapshot", "optional"])
def test_error_diagnostics_redact_request_urls_and_keys(tools, store, monkeypatch, path):
    from tradingagents.dataflows import alpha_vantage_indicator, interface

    secret = "test-rejected-key"
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", secret)
    error = RuntimeError(
        f"Key {secret} failed: https://www.alphavantage.co/query?apikey={secret}"
    )

    def fail(*args):
        raise error

    stage = "analyst/news" if path == "optional" else "analyst/market"
    run = _create_evidence_run(store, stage=stage, frozen_config={
        "tool_vendors": {"get_indicators": "alpha_vantage", "get_macro_indicators": "fred"},
    })
    if path == "indicator":
        monkeypatch.setattr(alpha_vantage_indicator, "_make_api_request", fail)
        result = tools.get_indicators("AAPL", "rsi", "2026-09-15", run_id=run.run_id)
    elif path == "snapshot":
        monkeypatch.setattr(data, "build_verified_market_snapshot", fail)
        result = tools.get_verified_market_snapshot("AAPL", "2026-09-15", run_id=run.run_id)
    else:
        monkeypatch.setitem(interface.VENDOR_METHODS, "get_macro_indicators", {"fred": fail})
        result = tools.get_macro_indicators("cpi", "2026-09-15", run_id=run.run_id)

    assert result.status == "error" and result.retryable
    assert secret not in result.model_dump_json()
    assert "https://" not in result.model_dump_json()
    assert store.list_evidence(run.run_id) == []


def test_finalize_analysis_is_public_and_strict(store, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        data.WorkflowService,
        "finalize_analysis",
        lambda _self, run_id: SimpleNamespace(
            run_id=run_id,
            status="completed",
            decision_id=run_id,
            rating="Hold",
            report_dir="/reports/run",
            complete_report_path="/reports/run/complete_report.md",
            section_paths=["/reports/run/complete_report.md"],
            changed=True,
        ),
    )
    tools = data.PluginTools(store)
    result = tools.finalize_analysis("run-1")

    assert result.work_type == "analysis"
    assert result.decision_id == "run-1"
    assert tools.finalize_analysis in tools.public_operations()
    with pytest.raises(ValidationError):
        data.AnalysisFinalizationResult.model_validate({**result.model_dump(), "extra": True})
