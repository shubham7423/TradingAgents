"""Retrieve and normalize Yahoo Finance option chains."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
import re

from pydantic import BaseModel, ConfigDict, Field

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


class AssessmentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    snapshot_id: str
    contract_symbol: str
    rationale: str = Field(min_length=1)
    expiry_rationale: str = Field(min_length=1)
    failure_condition: str = Field(min_length=1)


def evaluate_assessment(
    run_id: str, ticker: str, thesis: str, candidates: list[dict], preferred: str | None,
    no_trade_reason: str | None, snapshots: list[dict], verified_close: float | None,
) -> dict:
    """Validate saved option quotes and calculate deterministic per-contract economics."""
    if not isinstance(thesis, str) or not thesis.strip():
        raise ValueError("thesis is required")
    if len(candidates) > 2:
        raise ValueError("at most one candidate per strategy is allowed")
    parsed = [AssessmentCandidate.model_validate(item) for item in candidates]
    strategies = [item.strategy for item in parsed]
    if any(strategy not in {"cash_secured_put", "long_call"} for strategy in strategies):
        raise ValueError("unsupported option strategy")
    if len(set(strategies)) != len(strategies):
        raise ValueError("at most one candidate per strategy is allowed")
    if preferred is not None and preferred not in strategies:
        raise ValueError("preferred must name a submitted strategy")
    if not parsed and (not isinstance(no_trade_reason, str) or not no_trade_reason.strip()):
        raise ValueError("no_trade_reason is required when there are no candidates")

    by_id = {snapshot["snapshot_id"]: snapshot for snapshot in snapshots}
    result_candidates = []
    for candidate in parsed:
        snapshot = by_id.get(candidate.snapshot_id)
        if snapshot is None or snapshot.get("run_id") != run_id:
            raise ValueError("candidate must reference a saved option snapshot for this run")
        try:
            fetched = datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
            expiry = date.fromisoformat(snapshot["expiration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("snapshot has invalid fetch time or expiration") from exc
        if expiry < fetched.date():
            raise ValueError("contract is expired")
        occ = re.fullmatch(r"([A-Z][A-Z0-9.-]*)(\d{6})([CP])(\d{8})", candidate.contract_symbol)
        if occ is None:
            raise ValueError("contract symbol must be a standard OCC option symbol")
        root, expiry_code, side_code, strike_code = occ.groups()
        if root != ticker or expiry_code != expiry.strftime("%y%m%d"):
            raise ValueError("contract symbol root or expiration does not match snapshot")
        expected_side = "put" if candidate.strategy == "cash_secured_put" else "call"
        if side_code != ("P" if expected_side == "put" else "C"):
            raise ValueError("contract side does not match strategy")
        rows = [row for row in snapshot["payload"].get("contracts", [])
                if row.get("contract_symbol") == candidate.contract_symbol]
        if len(rows) != 1:
            raise ValueError("contract is absent or duplicated in saved snapshot")
        row = rows[0]
        if row.get("side") != expected_side or row.get("contract_size") != "REGULAR":
            raise ValueError("only matching standard 100-share contracts are supported")
        if row.get("currency") != "USD":
            raise ValueError("only USD contracts are supported")
        try:
            strike, bid, ask = (Decimal(str(row[name])) for name in ("strike", "bid", "ask"))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ValueError("strike, bid, and ask must be finite positive quotes") from exc
        if (not all(value.is_finite() and value > 0 for value in (strike, bid, ask))
                or bid > ask or strike * 1000 != Decimal(strike_code)):
            raise ValueError("invalid quote or strike does not match contract symbol")
        warnings = []
        midpoint = (bid + ask) / 2
        if (ask - bid) / midpoint > Decimal("0.20"):
            warnings.append("Bid/ask spread exceeds 20% of midpoint.")
        for field in ("volume", "open_interest"):
            if row.get(field) in (None, 0):
                warnings.append(f"{field.replace('_', ' ').title()} is zero or unknown.")
        last_trade = row.get("last_trade_at")
        if not last_trade:
            warnings.append("Last trade time is unknown; this is not quote age.")
        else:
            try:
                traded = datetime.fromisoformat(last_trade.replace("Z", "+00:00"))
                fetched_utc = fetched.replace(tzinfo=timezone.utc) if fetched.tzinfo is None else fetched
                traded_utc = traded.replace(tzinfo=timezone.utc) if traded.tzinfo is None else traded
                if fetched_utc - traded_utc > timedelta(days=7):
                    warnings.append("Last trade was more than seven days before quote fetch; this is not quote age.")
            except (TypeError, ValueError):
                warnings.append("Last trade time is unknown; this is not quote age.")
        underlying = snapshot["payload"].get("underlying", {})
        spot = underlying.get("regularMarketPrice")
        if verified_close is not None and spot is not None:
            try:
                spot_value, close_value = Decimal(str(spot)), Decimal(str(verified_close))
                if close_value > 0 and abs(spot_value - close_value) / close_value > Decimal("0.05"):
                    warnings.append("Provider underlying differs from verified stock close by more than 5%.")
            except (InvalidOperation, TypeError):
                warnings.append("Provider underlying comparison unavailable.")
        else:
            warnings.append("Provider underlying comparison unavailable.")
        output = {"strategy": candidate.strategy, "snapshot_id": candidate.snapshot_id,
                  "contract_symbol": candidate.contract_symbol, "expiration": expiry.isoformat(),
                  "strike": float(strike), "bid": float(bid), "ask": float(ask),
                  "quote_source": "Yahoo Finance", "fetched_at": snapshot["fetched_at"],
                  "last_trade_at": last_trade, "spread": float(ask - bid),
                  "volume": row.get("volume"), "open_interest": row.get("open_interest"),
                  "rationale": candidate.rationale, "expiry_rationale": candidate.expiry_rationale,
                  "failure_condition": candidate.failure_condition, "warnings": warnings}
        if candidate.strategy == "cash_secured_put":
            output.update(collateral=float((strike * 100).quantize(Decimal("0.01"))),
                          credit=float((bid * 100).quantize(Decimal("0.01"))),
                          breakeven=float((strike - bid).quantize(Decimal("0.01"))),
                          maximum_loss=float((strike * 100 - bid * 100).quantize(Decimal("0.01"))))
        else:
            output.update(debit=float((ask * 100).quantize(Decimal("0.01"))),
                          maximum_loss=float((ask * 100).quantize(Decimal("0.01"))),
                          breakeven=float((strike + ask).quantize(Decimal("0.01"))))
        result_candidates.append(output)
    return {"run_id": run_id, "ticker": ticker, "thesis": thesis.strip(),
            "preferred": preferred, "no_trade_reason": no_trade_reason,
            "candidates": result_candidates,
            "warnings": ["Figures exclude fees and do not promise fills.",
                         "No probability of profit is estimated."],
            "verified_close": verified_close}


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
