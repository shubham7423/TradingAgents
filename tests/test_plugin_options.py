import json
from types import SimpleNamespace

import pandas as pd
import pytest

import tradingagents.plugin.options as options


def test_evaluate_assessment_calculates_standard_option_economics():
    run_id = "run-1"
    put = {"strategy": "cash_secured_put", "snapshot_id": "snapshot-1",
           "contract_symbol": "AAPL261016P00150000", "rationale": "Entry below support",
           "expiry_rationale": "Allows thesis time", "failure_condition": "Support breaks"}
    snapshot = {"snapshot_id": "snapshot-1", "run_id": run_id, "expiration": "2026-10-16",
                "fetched_at": "2026-09-27T14:00:00Z", "payload": {"contracts": [
                    {"contract_symbol": "AAPL261016P00150000", "side": "put", "strike": 150,
                     "bid": 2.0, "ask": 2.2, "currency": "USD", "contract_size": "REGULAR"}]}}
    result = options.evaluate_assessment(run_id, "AAPL", "Bullish above support", [put],
                                         "cash_secured_put", None, [snapshot], None)
    assert result["candidates"][0]["collateral"] == 15000
    assert result["candidates"][0]["credit"] == 200
    assert result["candidates"][0]["breakeven"] == 148
    assert result["candidates"][0]["maximum_loss"] == 14800


def test_evaluate_assessment_calculates_long_call_and_supports_no_trade():
    call = {"strategy": "long_call", "snapshot_id": "snapshot-1",
            "contract_symbol": "AAPL261016C00160000", "rationale": "Breakout continuation",
            "expiry_rationale": "Allows thesis time", "failure_condition": "Breakout fails"}
    snapshot = {"snapshot_id": "snapshot-1", "run_id": "run-1", "expiration": "2026-10-16",
                "fetched_at": "2026-09-27T14:00:00Z", "payload": {"underlying": {}, "contracts": [
                    {"contract_symbol": "AAPL261016C00160000", "side": "call", "strike": 160,
                     "bid": 3.8, "ask": 4, "currency": "USD", "contract_size": "REGULAR"}]}}
    result = options.evaluate_assessment("run-1", "AAPL", "Breakout thesis", [call],
                                         "long_call", None, [snapshot], None)
    assert result["candidates"][0]["debit"] == 400
    assert result["candidates"][0]["maximum_loss"] == 400
    assert result["candidates"][0]["breakeven"] == 164
    no_trade = options.evaluate_assessment("run-1", "AAPL", "Wait", [], None,
                                            "No suitable contracts", [], None)
    assert no_trade["candidates"] == []
    assert no_trade["no_trade_reason"] == "No suitable contracts"


def test_fetch_chain_preserves_bid_ask_and_metadata(monkeypatch):
    put = {
        "contractSymbol": "AAPL261016P00150000", "strike": 150.0,
        "bid": 2.1, "ask": 2.3, "contractSize": "REGULAR", "currency": "USD",
        "lastTradeDate": pd.Timestamp("2026-09-27T14:00:00Z"),
    }
    call = {
        "contractSymbol": "AAPL261016C00160000", "strike": 160.0,
        "bid": 1.2, "ask": 1.4, "contractSize": "REGULAR", "currency": "USD",
        "lastTradeDate": pd.Timestamp("2026-09-27T14:01:00Z"),
    }
    fake = SimpleNamespace(
        options=("2026-10-16",),
        option_chain=lambda expiry: SimpleNamespace(
            puts=pd.DataFrame([put]), calls=pd.DataFrame([call]),
            underlying={"regularMarketPrice": 155.0},
        ),
    )
    monkeypatch.setattr(options.yf, "Ticker", lambda ticker: fake)

    result = options.fetch_chain("AAPL", "2026-10-16")

    assert [c["side"] for c in result["contracts"]] == ["put", "call"]
    assert result["contracts"][0]["bid"] == 2.1
    assert result["contracts"][0]["ask"] == 2.3
    assert result["contracts"][0]["currency"] == "USD"
    assert result["contracts"][0]["contract_size"] == "REGULAR"
    assert result["contracts"][0]["last_trade_at"] == "2026-09-27T14:00:00+00:00"
    assert result["contracts"][0]["volume"] is None
    assert result["contracts"][1]["bid"] == 1.2
    assert result["contracts"][1]["ask"] == 1.4
    assert result["contracts"][1]["currency"] == "USD"
    assert result["contracts"][1]["contract_size"] == "REGULAR"
    assert result["contracts"][1]["last_trade_at"] == "2026-09-27T14:01:00+00:00"
    json.dumps(result)


def test_fetch_chain_rejects_unknown_expiration_and_keeps_empty_chain(monkeypatch):
    fake = SimpleNamespace(
        options=("2026-10-16",),
        option_chain=lambda expiry: SimpleNamespace(puts=pd.DataFrame(), calls=pd.DataFrame(), underlying={}),
    )
    monkeypatch.setattr(options.yf, "Ticker", lambda ticker: fake)

    with pytest.raises(ValueError, match="unavailable"):
        options.fetch_chain("AAPL", "2026-10-23")
    assert options.fetch_expirations("AAPL") == ("2026-10-16",)
    assert options.fetch_chain("AAPL", "2026-10-16")["contracts"] == []
