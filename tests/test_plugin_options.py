import json
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

import tradingagents.plugin.options as options


def _assessment_fixture():
    candidate = {"strategy": "cash_secured_put", "snapshot_id": "snapshot-1",
                 "contract_symbol": "AAPL261016P00150000", "rationale": "Entry below support",
                 "expiry_rationale": "Allows thesis time", "failure_condition": "Support breaks"}
    snapshot = {"snapshot_id": "snapshot-1", "run_id": "run-1", "expiration": "2026-10-16",
                "fetched_at": "2026-09-27T14:00:00Z", "payload": {"underlying": {}, "contracts": [{
                    "contract_symbol": "AAPL261016P00150000", "side": "put", "strike": 150,
                    "bid": 2, "ask": 2.2, "currency": "USD", "contract_size": "REGULAR",
                    "volume": 5, "open_interest": 10, "last_trade_at": "2026-09-27T13:00:00Z"}]}}
    return candidate, snapshot


def _evaluate(candidate, snapshot, preferred="cash_secured_put", verified_close=None):
    return options.evaluate_assessment("run-1", "AAPL", "Bullish thesis", [candidate], preferred,
                                       None, [snapshot], verified_close)


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


@pytest.mark.parametrize(("change", "message"), [
    ("adjusted_root", "root or expiration"),
    ("contract_size", "standard 100-share"),
    ("currency", "USD"),
    ("missing_bid", "finite positive"),
    ("crossed", "invalid quote"),
    ("zero_bid", "invalid quote"),
    ("nonfinite_ask", "invalid quote"),
    ("absent_symbol", "absent or duplicated"),
    ("wrong_side", "matching standard"),
    ("expired", "expired"),
])
def test_evaluate_assessment_rejects_invalid_contract_evidence(change, message):
    candidate, snapshot = _assessment_fixture()
    row = snapshot["payload"]["contracts"][0]
    if change == "adjusted_root":
        row["contract_symbol"] = candidate["contract_symbol"] = "AAPL1261016P00150000"
    elif change == "contract_size":
        row["contract_size"] = "MINI"
    elif change == "currency":
        row["currency"] = "CAD"
    elif change == "missing_bid":
        del row["bid"]
    elif change == "crossed":
        row["bid"], row["ask"] = 2.3, 2.2
    elif change == "zero_bid":
        row["bid"] = 0
    elif change == "nonfinite_ask":
        row["ask"] = float("inf")
    elif change == "absent_symbol":
        row["contract_symbol"] = "AAPL261016P00151000"
    elif change == "wrong_side":
        row["side"] = "call"
    elif change == "expired":
        snapshot["expiration"] = "2026-09-26"
    with pytest.raises(ValueError, match=message):
        _evaluate(candidate, snapshot)


def test_evaluate_assessment_rejects_duplicate_strategy_and_unknown_preferred():
    candidate, snapshot = _assessment_fixture()
    with pytest.raises(ValueError, match="one candidate per strategy"):
        options.evaluate_assessment("run-1", "AAPL", "Thesis", [candidate, deepcopy(candidate)],
                                    "cash_secured_put", None, [snapshot], None)
    with pytest.raises(ValueError, match="preferred"):
        _evaluate(candidate, snapshot, preferred="long_call")


@pytest.mark.parametrize(("field", "value", "warning"), [
    ("bid", 2, "Bid/ask spread exceeds 20%"),
    ("volume", 0, "Volume is zero or unknown"),
    ("volume", None, "Volume is zero or unknown"),
    ("open_interest", 0, "Open Interest is zero or unknown"),
    ("open_interest", None, "Open Interest is zero or unknown"),
])
def test_evaluate_assessment_liquidity_warnings(field, value, warning):
    candidate, snapshot = _assessment_fixture()
    snapshot["payload"]["contracts"][0][field] = value
    if field == "bid":
        snapshot["payload"]["contracts"][0]["ask"] = 3
    warnings = _evaluate(candidate, snapshot)["candidates"][0]["warnings"]
    assert any(warning in item for item in warnings)


@pytest.mark.parametrize(("last_trade", "stale"), [
    ("2026-09-20T14:00:00Z", False),
    ("2026-09-20T13:59:59Z", True),
])
def test_evaluate_assessment_last_trade_uses_exact_seven_day_boundary(last_trade, stale):
    candidate, snapshot = _assessment_fixture()
    snapshot["payload"]["contracts"][0]["last_trade_at"] = last_trade
    warnings = _evaluate(candidate, snapshot)["candidates"][0]["warnings"]
    assert any("more than seven days" in item for item in warnings) is stale


def test_evaluate_assessment_warns_when_provider_spot_differs_from_verified_close():
    candidate, snapshot = _assessment_fixture()
    snapshot["payload"]["underlying"]["regularMarketPrice"] = 106
    warnings = _evaluate(candidate, snapshot, verified_close=100)["candidates"][0]["warnings"]
    assert any("differs from verified stock close" in item for item in warnings)


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
