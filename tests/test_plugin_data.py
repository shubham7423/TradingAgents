import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.interface import VendorRouteResult
from tradingagents.plugin import data
from tradingagents.plugin.store import PluginStore, RequestIdConflict

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
    frozen_config: dict | None = None,
):
    record, _ = store.create_run(
        request_id=request_id,
        request_hash=request_id,
        normalized_inputs={
            "ticker": "AAPL",
            "analysis_date": "2026-09-15",
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
        instrument={"canonical_symbol": "AAPL", "source": "yfinance"},
        lessons="",
        now="2026-09-15T12:00:00+00:00",
    )
    return record


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
    assert loaded.model_dump(exclude={"evidence"}) == started.model_dump(exclude={"evidence"})
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
def test_invalid_start_request_does_not_persist(change, tools, store):
    values = {"request_id": REQUEST_ID, "ticker": "AAPL", **change}

    with pytest.raises((ValidationError, ValueError)):
        tools.start_analysis(**values)

    assert _run_count(store) == 0


def test_canonical_digest_ignores_mapping_order():
    left = {"ticker": "AAPL", "options": {"b": 2, "a": 1}}
    right = {"options": {"a": 1, "b": 2}, "ticker": "AAPL"}

    assert data._canonical_json(left) == data._canonical_json(right)
    assert data._digest(left) == data._digest(right)


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

    assert result.content == '{"canonical_symbol":"AAPL","source":"yfinance"}'
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
