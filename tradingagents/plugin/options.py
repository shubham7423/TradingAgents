"""Retrieve and normalize Yahoo Finance option chains."""

from datetime import date, datetime
import math

import pandas as pd
import yfinance as yf


_FIELDS = {
    "contract_symbol": "contractSymbol",
    "strike": "strike",
    "bid": "bid",
    "ask": "ask",
    "volume": "volume",
    "open_interest": "openInterest",
    "implied_volatility": "impliedVolatility",
    "currency": "currency",
    "contract_size": "contractSize",
    "last_trade_at": "lastTradeDate",
}


def _scalar(value):
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return stamp.tz_convert("UTC").isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _rows(frame, side):
    contracts = []
    for row in frame.to_dict(orient="records"):
        contract = {name: _scalar(row.get(source)) for name, source in _FIELDS.items()}
        contract["side"] = side
        contracts.append(contract)
    return contracts


def fetch_expirations(ticker: str) -> tuple[str, ...]:
    """Return the expirations listed by Yahoo Finance for ticker."""
    return tuple(yf.Ticker(ticker).options)


def fetch_chain(ticker: str, expiration: str) -> dict:
    """Fetch one listed expiration and return JSON-safe quote evidence."""
    stock = yf.Ticker(ticker)
    if expiration not in stock.options:
        raise ValueError(f"expiration {expiration} is unavailable for {ticker}")
    chain = stock.option_chain(expiration)
    return {
        "expiration": expiration,
        "underlying": {key: _scalar(value) for key, value in (chain.underlying or {}).items()},
        "contracts": [*_rows(chain.puts, "put"), *_rows(chain.calls, "call")],
    }
