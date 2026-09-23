# TradingAgents/graph/reflection.py

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

import yfinance as yf

from tradingagents.dataflows.symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    raw_return: float
    alpha_return: float
    holding_sessions: int
    benchmark: str
    resolution_date: str


def resolve_benchmark(config: Mapping[str, Any], ticker: str) -> str:
    if config.get("benchmark_ticker"):
        return str(config["benchmark_ticker"])
    benchmark_map = config.get("benchmark_map", {})
    ticker_upper = ticker.upper()
    for suffix, benchmark in benchmark_map.items():
        if suffix and ticker_upper.endswith(str(suffix).upper()):
            return str(benchmark)
    return str(benchmark_map.get("", "SPY"))


def calculate_outcome(
    ticker: str,
    trade_date: str,
    config: Mapping[str, Any],
    holding_sessions: int = 5,
    as_of_date: str | None = None,
) -> Outcome | None:
    if holding_sessions != 5:
        raise ValueError("holding_sessions must be 5")
    benchmark = resolve_benchmark(config, ticker)
    try:
        start = datetime.strptime(trade_date, "%Y-%m-%d")
        end = start + timedelta(days=holding_sessions + 7)
        stock = yf.Ticker(normalize_symbol(ticker)).history(
            start=trade_date, end=end.strftime("%Y-%m-%d")
        )
        bench = yf.Ticker(benchmark).history(
            start=trade_date, end=end.strftime("%Y-%m-%d")
        )
        if len(stock) <= holding_sessions or len(bench) <= holding_sessions:
            return None
        resolution_date = stock.index[holding_sessions].strftime("%Y-%m-%d")
        if as_of_date is not None and resolution_date > as_of_date:
            return None
        stock_start, stock_end = (
            float(stock["Close"].iloc[index]) for index in (0, holding_sessions)
        )
        benchmark_start, benchmark_end = (
            float(bench["Close"].iloc[index]) for index in (0, holding_sessions)
        )
        prices = (stock_start, stock_end, benchmark_start, benchmark_end)
        if any(not isfinite(price) or price <= 0 for price in prices):
            return None
        raw = float(
            (stock_end - stock_start) / stock_start
        )
        benchmark_return = (benchmark_end - benchmark_start) / benchmark_start
        return Outcome(raw, raw - benchmark_return, holding_sessions, benchmark, resolution_date)
    except Exception as exc:
        logger.warning(
            "Could not resolve outcome for %s on %s vs %s (will retry next run): %s",
            ticker, trade_date, benchmark, exc,
        )
        return None


def get_log_reflection_prompt() -> str:
    """Return the concise system prompt for deferred decision reflections."""
    return (
        "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
        "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
        "Cover in order:\n"
        "1. Was the directional call correct? (cite the alpha figure)\n"
        "2. Which part of the investment thesis held or failed?\n"
        "3. One concrete lesson to apply to the next similar analysis.\n\n"
        "Be specific and terse. Your output will be stored verbatim in a decision log "
        "and re-read by future analysts, so every word must earn its place."
    )


def build_reflection_messages(
    final_decision: str,
    raw_return: float,
    alpha_return: float,
    benchmark_name: str = "SPY",
    *,
    system_prompt: str | None = None,
) -> list[tuple[str, str]]:
    """Build the model messages for a deferred decision reflection without I/O."""
    return [
        ("system", get_log_reflection_prompt() if system_prompt is None else system_prompt),
        (
            "human",
            (
                f"Raw return: {raw_return:+.1%}\n"
                f"Alpha vs {benchmark_name}: {alpha_return:+.1%}\n\n"
                f"Final Decision:\n{final_decision}"
            ),
        ),
    ]


class Reflector:
    """Handles reflection on trading decisions."""

    def __init__(self, quick_thinking_llm: Any):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm
        self.log_reflection_prompt = self._get_log_reflection_prompt()

    def _get_log_reflection_prompt(self) -> str:
        """Concise prompt for reflect_on_final_decision (Phase B log entries).

        Produces 2-4 sentences of plain prose — compact enough to be re-injected
        into future agent prompts without bloating the context window.
        """
        return get_log_reflection_prompt()

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
    ) -> str:
        """Single reflection call on the final trade decision with outcome context.

        Used by Phase B deferred reflection. The final_trade_decision already
        synthesises all analyst insights, so no separate market context is needed.
        ``benchmark_name`` is the label used for the alpha line (e.g. ``"SPY"``
        for US tickers, ``"^N225"`` for ``.T`` listings); defaults to SPY for
        callers that haven't been updated to thread the benchmark through.
        """
        messages = build_reflection_messages(
            final_decision,
            raw_return,
            alpha_return,
            benchmark_name,
            system_prompt=self.log_reflection_prompt,
        )
        return self.quick_thinking_llm.invoke(messages).content
