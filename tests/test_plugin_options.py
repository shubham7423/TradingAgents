import json
from types import SimpleNamespace

import pandas as pd
import pytest

import tradingagents.plugin.options as options


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
