import sqlite3

import pytest
from pydantic import ValidationError

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
