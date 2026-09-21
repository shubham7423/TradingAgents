from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from threading import Lock
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    resolve_instrument_identity,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import get_config, replace_config
from tradingagents.dataflows.interface import (
    TOOLS_CATEGORIES,
    VENDOR_METHODS,
    VendorRouteResult,
    route_to_vendor_traced,
    sanitize_diagnostic,
)
from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot
from tradingagents.dataflows.reddit import fetch_reddit_posts as _fetch_reddit_posts
from tradingagents.dataflows.stocktwits import (
    fetch_stocktwits_messages as _fetch_stocktwits_messages,
)
from tradingagents.dataflows.symbol_utils import NoMarketDataError, crypto_base, normalize_symbol
from tradingagents.dataflows.utils import get_current_date, safe_ticker_component
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.plugin.store import (
    AnalysisSnapshot,
    EvidenceRecord,
    PluginStore,
    RunNotFound,
    RunRecord,
    StageOutputRecord,
    canonical_json,
    digest_json,
)
from tradingagents.plugin.workflow import WorkflowService, _require_compatible_run, describe_stage

AnalystKey = Literal["market", "social", "news", "fundamentals"]
AssetType = Literal["stock", "crypto"]
MAX_ROUNDS = 10
MAX_PAGE_SIZE = 32_000
_DATA_LOCK = Lock()
resolve_instrument_identity_data = resolve_instrument_identity

DATA_CONFIG_KEYS = (
    "data_cache_dir",
    "news_article_limit",
    "global_news_article_limit",
    "global_news_lookback_days",
    "global_news_queries",
    "data_vendors",
    "tool_vendors",
)

STAGE_TOOLS = {
    "analyst/market": {"get_stock_data", "get_indicators", "get_verified_market_snapshot"},
    "analyst/social": {"get_news", "fetch_stocktwits_messages", "fetch_reddit_posts"},
    "analyst/news": {
        "get_news",
        "get_global_news",
        "get_insider_transactions",
        "get_macro_indicators",
        "get_prediction_markets",
    },
    "analyst/fundamentals": {
        "get_fundamentals",
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
    },
}


def _encode_cursor(evidence_id: str, offset: int) -> str:
    payload = canonical_json({"evidence_id": evidence_id, "offset": offset}).encode()
    return base64.urlsafe_b64encode(payload).decode()


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"evidence_id", "offset"}:
            raise ValueError
        if not isinstance(payload["evidence_id"], str):
            raise ValueError
        evidence_id = str(UUID(payload["evidence_id"]))
        offset = payload["offset"]
        if type(offset) is not int or offset < 0 or cursor != _encode_cursor(evidence_id, offset):
            raise ValueError
    except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid evidence cursor") from exc
    return evidence_id, offset


def _encode_analysis_cursor(created_at: str, run_id: str) -> str:
    payload = canonical_json({"created_at": created_at, "run_id": run_id}).encode()
    return base64.urlsafe_b64encode(payload).decode()


def _decode_analysis_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"created_at", "run_id"}:
            raise ValueError
        created_at = payload["created_at"]
        if not isinstance(created_at, str):
            raise ValueError
        run_id = str(UUID(payload["run_id"]))
        if cursor != _encode_analysis_cursor(created_at, run_id):
            raise ValueError
    except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid analysis cursor") from exc
    return created_at, run_id


def _page_size(value: int | None) -> int:
    if value is None:
        return MAX_PAGE_SIZE
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    return value


def _serialize_content(content: object) -> tuple[str, Literal["text", "json"]]:
    if isinstance(content, str):
        return content, "text"
    if isinstance(content, (Mapping, list)):
        return canonical_json(content), "json"
    if content is None:
        return "", "text"
    return str(content), "text"


def _classify_result(
    result: VendorRouteResult,
) -> tuple[
    Literal["success", "no_data", "unavailable", "error"],
    str,
    Literal["text", "json"],
    list[str],
    bool,
]:
    content, content_format = _serialize_content(result.content)
    status = result.status
    warnings = [sanitize_diagnostic(warning) for warning in result.warnings]
    retryable = result.retryable

    malformed_payload = False
    candidate = result.content if isinstance(result.content, Mapping) else None
    if candidate is None and content_format == "text" and content.lstrip().startswith("{"):
        try:
            candidate = json.loads(content)
        except json.JSONDecodeError:
            candidate = None
    if isinstance(candidate, dict) and any(
        field in candidate for field in ("Error Message", "Information", "Note")
    ):
        malformed_payload = True

    failure_sentinel = (
        content.startswith(("Error fetching", "Error retrieving", "Error:"))
        or "currently unavailable (network error" in content
        or content.startswith("<stocktwits unavailable")
    )

    if status != "success" or malformed_payload or failure_sentinel:
        content = sanitize_diagnostic(content)

    if content.startswith("<Reddit unavailable: every source failed"):
        status, retryable = "error", True
    else:
        failed_sources = re.findall(
            r"r/[A-Za-z0-9_]+(?=: <unavailable: fetch failed)", content
        )
        failed_summary = re.search(r"<unavailable \(fetch failed\): ([^>]+)>", content)
        if failed_summary:
            failed_sources.extend(re.findall(r"r/[A-Za-z0-9_]+", failed_summary.group(1)))
        failed_sources = list(dict.fromkeys(failed_sources))
        if failed_sources:
            status, retryable = "success", False
            warnings.append(f"Reddit sources unavailable: {', '.join(failed_sources)}")
        elif (
            content.startswith("NO_DATA_AVAILABLE")
            or content.startswith("No news found")
            or content.startswith("No global news found")
            or content.startswith("No open prediction markets")
            or content.startswith("<no Reddit posts")
        ):
            status, retryable = "no_data", False
        elif "public stream serves only recent messages" in content:
            status, retryable = "unavailable", False
        elif failure_sentinel or malformed_payload:
            status, retryable = "error", True

    return status, content, content_format, warnings, retryable


def _page_record(
    record: EvidenceRecord,
    *,
    cursor: str | None,
    page_size: int | None,
    reused: bool,
) -> DataToolResult:
    size = _page_size(page_size)
    offset = 0
    if cursor is not None:
        evidence_id, offset = _decode_cursor(cursor)
        if evidence_id != record.evidence_id or offset >= len(record.content):
            raise ValueError("cursor does not match saved evidence")
    end = min(offset + size, len(record.content))
    complete = end == len(record.content)
    return DataToolResult(
        status=record.status,
        content=record.content[offset:end],
        content_format=record.content_format,
        source=record.source.get("vendor", "unknown"),
        warnings=record.warnings,
        fetched_at=record.fetched_at,
        reused=reused,
        retryable=False,
        evidence_id=record.evidence_id,
        run_id=record.run_id,
        stage_id=record.stage_id,
        page=EvidencePage(
            cursor=cursor,
            next_cursor=None if complete else _encode_cursor(record.evidence_id, end),
            complete=complete,
        ),
    )


def _parse_date(value: str | None) -> str:
    return date.fromisoformat(value if value is not None else get_current_date()).isoformat()


def _validate_ticker(value: str) -> str:
    trimmed = value.strip()
    safe_ticker_component(trimmed)
    return normalize_symbol(trimmed)


def _validate_date(value: str, name: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a real date in YYYY-MM-DD format") from exc


def _validate_window(
    start_date: str | None, end_date: str | None, *, optional: bool = False
) -> tuple[str | None, str | None]:
    if start_date is None and end_date is None:
        if optional:
            return None, None
        raise ValueError("start_date and end_date are required")
    if (start_date is None) != (end_date is None):
        raise ValueError("start_date and end_date must be provided together")
    start = _validate_date(start_date, "start_date")
    end = _validate_date(end_date, "end_date")
    if start > end:
        raise ValueError("start_date must not be after end_date")
    return start, end


def _validate_limit(value: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError(f"{name} must be between 1 and 100")
    return value


def _no_market_data_content(error: NoMarketDataError) -> str:
    resolved = "" if error.canonical == error.symbol else f" (resolved to '{error.canonical}')"
    reason = f" ({error.detail})" if error.detail else ""
    return (
        f"NO_DATA_AVAILABLE: No usable market data for '{error.symbol}'{resolved} from "
        f"any configured vendor{reason}. The symbol may be invalid, delisted, not covered, "
        "or the vendor returned stale data. Do not estimate or fabricate values — report that "
        "data is unavailable for this symbol."
    )


def _validate_run_call(run: RunRecord, tool_name: str, arguments: dict) -> None:
    if run.status != "active":
        raise ValueError(f"analysis {run.run_id} is not active")
    if tool_name != "resolve_instrument_identity" and tool_name not in STAGE_TOOLS.get(
        run.current_stage, set()
    ):
        raise ValueError(f"tool {tool_name} is not allowed at stage {run.current_stage}")

    ticker = arguments.get("ticker", arguments.get("symbol"))
    if ticker is not None and _validate_ticker(ticker) != run.instrument["canonical_symbol"]:
        raise ValueError("ticker does not match the analysis instrument")

    analysis_date = date.fromisoformat(run.normalized_inputs["analysis_date"])
    if "curr_date" in arguments:
        curr_date = arguments["curr_date"]
        if curr_date is None or date.fromisoformat(curr_date) != analysis_date:
            raise ValueError("curr_date must match the analysis date")

    start_value = arguments.get("start_date")
    end_value = arguments.get("end_date")
    if "start_date" in arguments or "end_date" in arguments:
        _validate_window(start_value, end_value)
    start_date = date.fromisoformat(start_value) if start_value is not None else None
    end_date = date.fromisoformat(end_value) if end_value is not None else None
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if end_date is not None and end_date > analysis_date:
        raise ValueError("end_date must not be after the analysis date")


def _vendor_chain(value: str, available: set[str]) -> str:
    vendors = [vendor.strip() for vendor in value.split(",")]
    if not all(vendors):
        raise ValueError("vendor chains cannot contain blank entries")
    if len(vendors) != len(set(vendors)):
        raise ValueError("vendor chains cannot contain duplicates")
    if "default" in vendors:
        if len(vendors) != 1:
            raise ValueError("default must be the only vendor in a chain")
        return "default"
    unavailable = [vendor for vendor in vendors if vendor not in available]
    if unavailable:
        raise ValueError(f"vendors not available for this route: {unavailable}")
    return ",".join(vendors)


def _canonical_vendor_overrides(overrides: VendorOverrides) -> VendorOverrides:
    categories = {}
    for category, chain in overrides.categories.items():
        if category not in TOOLS_CATEGORIES:
            raise ValueError(f"unknown vendor category: {category}")
        tools = TOOLS_CATEGORIES[category]["tools"]
        available = set.intersection(*(set(VENDOR_METHODS[tool]) for tool in tools))
        categories[category] = _vendor_chain(chain, available)

    tools = {}
    for tool, chain in overrides.tools.items():
        if tool not in VENDOR_METHODS:
            raise ValueError(f"unknown vendor tool: {tool}")
        tools[tool] = _vendor_chain(chain, set(VENDOR_METHODS[tool]))
    return VendorOverrides(categories=categories, tools=tools)


class VendorOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)


class StartAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    ticker: str
    analysis_date: str | None = None
    asset_type: AssetType = "stock"
    analysts: list[AnalystKey] | None = None
    research_rounds: int = Field(default=1, ge=1, le=MAX_ROUNDS)
    risk_rounds: int = Field(default=1, ge=1, le=MAX_ROUNDS)
    output_language: str = Field(default="English", min_length=1, max_length=100)
    vendor_overrides: VendorOverrides = Field(default_factory=VendorOverrides)

    @model_validator(mode="after")
    def normalize(self) -> StartAnalysisRequest:
        self.request_id = str(UUID(self.request_id))
        self.analysts = list(ANALYST_NODE_SPECS) if self.analysts is None else self.analysts
        if not self.analysts:
            raise ValueError("at least one analyst must be selected")
        if len(self.analysts) != len(set(self.analysts)):
            raise ValueError("analysts cannot contain duplicates")
        self.output_language = self.output_language.strip()
        if not self.output_language:
            raise ValueError("output language cannot be blank")
        self.vendor_overrides = _canonical_vendor_overrides(self.vendor_overrides)
        return self


class EvidenceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    stage_id: str
    tool_name: str
    status: Literal["success", "no_data", "unavailable"]
    fetched_at: str
    source: dict
    warnings: list[str]


class EvidencePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None = None
    next_cursor: str | None = None
    complete: bool


class DataToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "no_data", "unavailable", "error"]
    content: str
    content_format: Literal["text", "json"]
    source: str
    warnings: list[str]
    fetched_at: str
    reused: bool
    retryable: bool
    evidence_id: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    page: EvidencePage


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    request_id: str
    status: Literal["active", "ready_to_finalize", "completed", "cancelled"]
    revision: int
    current_stage: str
    analysis_date: str
    analysts: list[AnalystKey]
    research_rounds: int
    risk_rounds: int
    output_language: str
    frozen_config: dict
    instrument: dict
    lessons: str
    evidence: list[EvidenceMetadata] = Field(default_factory=list)
    active_role: str | None = None
    instructions: str | list[dict[str, str]] | None = None
    required_evidence: list[EvidenceCheckResult] = Field(default_factory=list)
    output_schema: dict | None = None
    sections: list[StageOutputResult] = Field(default_factory=list)
    selected_section: StageOutputResult | None = None
    evidence_page: DataToolResult | None = None


class EvidenceCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, object]
    satisfied: bool


class StageOutputResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    stage_id: str
    role_key: str
    output_kind: Literal["text", "structured"]
    canonical_output: object
    rendered_output: str
    accepted_revision: int
    accepted_at: str


class AnalysisSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    ticker: str
    analysis_date: str
    status: Literal["active", "ready_to_finalize", "completed", "cancelled"]
    revision: int
    current_stage: str
    created_at: str
    updated_at: str


class AnalysisListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analyses: list[AnalysisSummary]
    next_cursor: str | None = None


class SubmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: Literal["active", "ready_to_finalize"]
    revision: int
    current_stage: str
    receipt: StageOutputResult


class CancellationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: Literal["cancelled"]
    revision: int
    current_stage: str
    changed: bool


class PluginTools:
    def __init__(self, store: PluginStore, server_config: dict | None = None):
        self._store = store
        self._workflow = WorkflowService(store)
        self._server_config = deepcopy(server_config if server_config is not None else get_config())

    def public_operations(self) -> tuple[Callable[..., object], ...]:
        return (
            self.start_analysis,
            self.get_analysis,
            self.submit_stage,
            self.list_analyses,
            self.cancel_analysis,
            self.get_stock_data,
            self.get_indicators,
            self.get_verified_market_snapshot,
            self.get_fundamentals,
            self.get_balance_sheet,
            self.get_cashflow,
            self.get_income_statement,
            self.resolve_instrument_identity,
            self.get_news,
            self.get_global_news,
            self.get_insider_transactions,
            self.get_macro_indicators,
            self.get_prediction_markets,
            self.fetch_stocktwits_messages,
            self.fetch_reddit_posts,
        )

    def get_stock_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        symbol = _validate_ticker(symbol)
        start_date, end_date = _validate_window(start_date, end_date)
        arguments = {"symbol": symbol, "start_date": start_date, "end_date": end_date}
        return self._execute(
            "get_stock_data",
            arguments,
            {"start": start_date, "end": end_date},
            lambda: route_to_vendor_traced(
                "get_stock_data", symbol, start_date, end_date
            ),
            run_id,
            cursor,
            page_size,
        )

    def get_indicators(
        self,
        symbol: str,
        indicator: str,
        curr_date: str,
        look_back_days: int = 30,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        symbol = _validate_ticker(symbol)
        curr_date = _validate_date(curr_date, "curr_date")
        if not indicator.strip() or "," in indicator:
            raise ValueError("indicator must specify one indicator per call")
        arguments = {
            "symbol": symbol,
            "indicator": indicator,
            "curr_date": curr_date,
            "look_back_days": look_back_days,
        }
        return self._execute(
            "get_indicators",
            arguments,
            {"end": curr_date, "look_back_days": look_back_days},
            lambda: route_to_vendor_traced(
                "get_indicators", symbol, indicator, curr_date, look_back_days
            ),
            run_id,
            cursor,
            page_size,
        )

    def get_verified_market_snapshot(
        self,
        symbol: str,
        curr_date: str,
        look_back_days: int = 30,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        symbol = _validate_ticker(symbol)
        curr_date = _validate_date(curr_date, "curr_date")
        arguments = {
            "symbol": symbol,
            "curr_date": curr_date,
            "look_back_days": look_back_days,
        }

        def fetch() -> VendorRouteResult:
            try:
                content = build_verified_market_snapshot(symbol, curr_date, look_back_days)
            except NoMarketDataError as exc:
                return VendorRouteResult(
                    content=_no_market_data_content(exc),
                    status="no_data",
                    source=None,
                    warnings=(f"NoMarketDataError: {exc}",),
                )
            except Exception as exc:  # noqa: BLE001 - expose retryable snapshot failures
                return VendorRouteResult(
                    content=(
                        "Error building verified market snapshot: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    status="error",
                    source=None,
                    warnings=(f"{type(exc).__name__}: {exc}",),
                    retryable=True,
                )
            return VendorRouteResult(content=content, status="success", source=None)

        return self._execute(
            "get_verified_market_snapshot",
            arguments,
            {"end": curr_date, "look_back_days": look_back_days},
            fetch,
            run_id,
            cursor,
            page_size,
        )

    def get_fundamentals(
        self,
        ticker: str,
        curr_date: str,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        ticker = _validate_ticker(ticker)
        curr_date = _validate_date(curr_date, "curr_date")
        arguments = {"ticker": ticker, "curr_date": curr_date}
        return self._execute(
            "get_fundamentals",
            arguments,
            {"end": curr_date},
            lambda: route_to_vendor_traced("get_fundamentals", ticker, curr_date),
            run_id,
            cursor,
            page_size,
        )

    def get_balance_sheet(
        self,
        ticker: str,
        freq: str = "quarterly",
        curr_date: str | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        return self._statement(
            "get_balance_sheet", ticker, freq, curr_date, run_id, cursor, page_size
        )

    def get_cashflow(
        self,
        ticker: str,
        freq: str = "quarterly",
        curr_date: str | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        return self._statement("get_cashflow", ticker, freq, curr_date, run_id, cursor, page_size)

    def get_income_statement(
        self,
        ticker: str,
        freq: str = "quarterly",
        curr_date: str | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        return self._statement(
            "get_income_statement", ticker, freq, curr_date, run_id, cursor, page_size
        )

    def _statement(
        self,
        tool_name: str,
        ticker: str,
        freq: str,
        curr_date: str | None,
        run_id: str | None,
        cursor: str | None,
        page_size: int | None,
    ) -> DataToolResult:
        ticker = _validate_ticker(ticker)
        if curr_date is not None:
            curr_date = _validate_date(curr_date, "curr_date")
        arguments = {"ticker": ticker, "freq": freq, "curr_date": curr_date}
        return self._execute(
            tool_name,
            arguments,
            {} if curr_date is None else {"end": curr_date},
            lambda: route_to_vendor_traced(tool_name, ticker, freq, curr_date),
            run_id,
            cursor,
            page_size,
        )

    def get_news(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        ticker = _validate_ticker(ticker)
        start_date, end_date = _validate_window(start_date, end_date)
        arguments = {"ticker": ticker, "start_date": start_date, "end_date": end_date}
        return self._execute(
            "get_news",
            arguments,
            {"start": start_date, "end": end_date},
            lambda: route_to_vendor_traced("get_news", ticker, start_date, end_date),
            run_id,
            cursor,
            page_size,
        )

    def get_global_news(
        self,
        curr_date: str,
        look_back_days: int | None = None,
        limit: int | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        curr_date = _validate_date(curr_date, "curr_date")
        arguments = {
            "curr_date": curr_date,
            "look_back_days": look_back_days,
            "limit": limit,
        }
        return self._execute(
            "get_global_news",
            arguments,
            {"end": curr_date, "look_back_days": look_back_days},
            lambda: route_to_vendor_traced(
                "get_global_news", curr_date, look_back_days, limit
            ),
            run_id,
            cursor,
            page_size,
        )

    def get_insider_transactions(
        self,
        ticker: str,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        ticker = _validate_ticker(ticker)
        arguments = {"ticker": ticker}
        return self._execute(
            "get_insider_transactions",
            arguments,
            {},
            lambda: self._historical_result(
                route_to_vendor_traced("get_insider_transactions", ticker), run_id
            ),
            run_id,
            cursor,
            page_size,
        )

    def get_macro_indicators(
        self,
        indicator: str,
        curr_date: str,
        look_back_days: int | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        curr_date = _validate_date(curr_date, "curr_date")
        arguments = {
            "indicator": indicator,
            "curr_date": curr_date,
            "look_back_days": look_back_days,
        }
        return self._execute(
            "get_macro_indicators",
            arguments,
            {"end": curr_date, "look_back_days": look_back_days},
            lambda: route_to_vendor_traced(
                "get_macro_indicators", indicator, curr_date, look_back_days
            ),
            run_id,
            cursor,
            page_size,
        )

    def get_prediction_markets(
        self,
        topic: str,
        limit: int | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        arguments = {"topic": topic, "limit": limit}
        return self._execute(
            "get_prediction_markets",
            arguments,
            {},
            lambda: self._historical_result(
                route_to_vendor_traced("get_prediction_markets", topic, limit), run_id
            ),
            run_id,
            cursor,
            page_size,
        )

    def _historical_result(
        self, result: VendorRouteResult, run_id: str | None
    ) -> VendorRouteResult:
        if run_id is None:
            return result
        analysis_date = self._store.get_run(run_id).normalized_inputs["analysis_date"]
        if analysis_date >= get_current_date():
            return result
        return VendorRouteResult(
            content=result.content,
            status=result.status,
            source=result.source,
            warnings=(
                *result.warnings,
                "historical coverage warning: this source has no point-in-time cutoff; "
                "results may include information published after the analysis date.",
            ),
            retryable=result.retryable,
            legacy_error=result.legacy_error,
        )

    def resolve_instrument_identity(
        self, ticker: str, run_id: str | None = None
    ) -> DataToolResult:
        requested = ticker.strip().upper()
        canonical = _validate_ticker(ticker)

        def fetch() -> VendorRouteResult:
            try:
                metadata = resolve_instrument_identity_data(canonical)
            except Exception:  # noqa: BLE001 - identity enrichment is best effort
                metadata = {}
            asset_type = "crypto" if crypto_base(canonical) else "stock"
            instrument = {
                "requested_symbol": requested,
                "canonical_symbol": canonical,
                "asset_type": asset_type,
                "metadata": metadata,
                "context": build_instrument_context(canonical, asset_type, metadata),
                "source": "yfinance" if metadata else "symbol_utils",
            }
            return VendorRouteResult(
                content=instrument,
                status="success",
                source=instrument["source"],
                warnings=() if metadata else ("yfinance identity metadata unavailable",),
            )

        return self._execute(
            "resolve_instrument_identity",
            {"ticker": ticker},
            {},
            fetch,
            run_id,
            None,
            None,
        )

    def fetch_stocktwits_messages(
        self,
        ticker: str,
        limit: int = 30,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        ticker = _validate_ticker(ticker)
        limit = _validate_limit(limit, "limit")
        start_date, end_date = _validate_window(start_date, end_date, optional=True)
        arguments = {
            "ticker": ticker,
            "limit": limit,
            "start_date": start_date,
            "end_date": end_date,
        }
        return self._execute(
            "fetch_stocktwits_messages",
            arguments,
            {} if start_date is None else {"start": start_date, "end": end_date},
            lambda: VendorRouteResult(
                content=_fetch_stocktwits_messages(
                    ticker, limit, start_date=start_date, end_date=end_date
                ),
                status="success",
                source="stocktwits",
            ),
            run_id,
            cursor,
            page_size,
        )

    def fetch_reddit_posts(
        self,
        ticker: str,
        limit_per_sub: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> DataToolResult:
        ticker = _validate_ticker(ticker)
        limit_per_sub = _validate_limit(limit_per_sub, "limit_per_sub")
        start_date, end_date = _validate_window(start_date, end_date, optional=True)
        arguments = {
            "ticker": ticker,
            "limit_per_sub": limit_per_sub,
            "start_date": start_date,
            "end_date": end_date,
        }
        return self._execute(
            "fetch_reddit_posts",
            arguments,
            {} if start_date is None else {"start": start_date, "end": end_date},
            lambda: VendorRouteResult(
                content=_fetch_reddit_posts(
                    ticker,
                    limit_per_sub=limit_per_sub,
                    start_date=start_date,
                    end_date=end_date,
                ),
                status="success",
                source="reddit",
            ),
            run_id,
            cursor,
            page_size,
        )

    def _execute(
        self,
        tool_name: str,
        arguments: dict,
        requested_window: dict,
        fetch: Callable[[], VendorRouteResult],
        run_id: str | None,
        cursor: str | None,
        page_size: int | None,
    ) -> DataToolResult:
        if run_id is None:
            if cursor is not None or page_size is not None:
                raise ValueError("standalone calls do not support cursor or page_size")
            with _DATA_LOCK:
                routed = fetch()
            status, content, content_format, warnings, retryable = _classify_result(routed)
            return DataToolResult(
                status=status,
                content=content,
                content_format=content_format,
                source=routed.source or "unknown",
                warnings=warnings,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                reused=False,
                retryable=retryable,
                page=EvidencePage(complete=True),
            )

        run = self._store.get_run(run_id)
        _validate_run_call(run, tool_name, arguments)
        if tool_name == "resolve_instrument_identity":
            if cursor is not None or page_size is not None:
                raise ValueError("instrument identity does not support cursor or page_size")
            content, content_format = _serialize_content(run.instrument)
            return DataToolResult(
                status="success",
                content=content,
                content_format=content_format,
                source=run.instrument.get("source", "unknown"),
                warnings=(
                    ["yfinance identity metadata unavailable"]
                    if run.instrument.get("source") == "symbol_utils"
                    else []
                ),
                fetched_at=run.created_at,
                reused=True,
                retryable=False,
                run_id=run.run_id,
                stage_id=run.current_stage,
                page=EvidencePage(complete=True),
            )

        _page_size(page_size)
        if cursor is not None:
            _decode_cursor(cursor)
        argument_hash = digest_json(arguments)
        existing = self._store.find_evidence(
            run.run_id, run.current_stage, tool_name, argument_hash
        )
        if existing is not None:
            return _page_record(existing, cursor=cursor, page_size=page_size, reused=True)
        if cursor is not None:
            raise ValueError("cursor does not match saved evidence")

        with _DATA_LOCK:
            existing = self._store.find_evidence(
                run.run_id, run.current_stage, tool_name, argument_hash
            )
            if existing is not None:
                return _page_record(existing, cursor=None, page_size=page_size, reused=True)

            previous_config = get_config()
            replace_config(run.frozen_config)
            try:
                routed = fetch()
            finally:
                replace_config(previous_config)

            status, content, content_format, warnings, retryable = _classify_result(routed)
            fetched_at = datetime.now(timezone.utc).isoformat()
            if status == "error":
                return DataToolResult(
                    status=status,
                    content=content,
                    content_format=content_format,
                    source=routed.source or "unknown",
                    warnings=warnings,
                    fetched_at=fetched_at,
                    reused=False,
                    retryable=retryable,
                    run_id=run.run_id,
                    stage_id=run.current_stage,
                    page=EvidencePage(complete=True),
                )

            record, created = self._store.save_evidence(
                run_id=run.run_id,
                expected_stage=run.current_stage,
                tool_name=tool_name,
                argument_hash=argument_hash,
                arguments=arguments,
                status=status,
                fetched_at=fetched_at,
                requested_window=requested_window,
                content=content,
                content_format=content_format,
                source={"vendor": routed.source or "unknown"},
                warnings=warnings,
            )
            return _page_record(record, cursor=None, page_size=page_size, reused=not created)

    def start_analysis(
        self,
        request_id: str,
        ticker: str,
        analysis_date: str | None = None,
        asset_type: AssetType = "stock",
        analysts: list[AnalystKey] | None = None,
        research_rounds: int = 1,
        risk_rounds: int = 1,
        output_language: str = "English",
        vendor_overrides: VendorOverrides | None = None,
    ) -> AnalysisResult:
        request = StartAnalysisRequest(
            request_id=request_id,
            ticker=ticker,
            analysis_date=analysis_date,
            asset_type=asset_type,
            analysts=analysts,
            research_rounds=research_rounds,
            risk_rounds=risk_rounds,
            output_language=output_language,
            vendor_overrides=(
                VendorOverrides() if vendor_overrides is None else vendor_overrides
            ),
        )
        canonical = _validate_ticker(request.ticker)
        resolved_date = _parse_date(request.analysis_date)
        overrides = request.vendor_overrides.model_dump()
        fingerprint = {
            "ticker": canonical,
            "analysis_date": request.analysis_date,
            "asset_type": request.asset_type,
            "analysts": request.analysts,
            "research_rounds": request.research_rounds,
            "risk_rounds": request.risk_rounds,
            "output_language": request.output_language,
            "vendor_overrides": overrides,
        }
        normalized_inputs = {**fingerprint, "analysis_date": resolved_date}

        metadata = resolve_instrument_identity(canonical)
        instrument = {
            "requested_symbol": request.ticker.strip().upper(),
            "canonical_symbol": canonical,
            "asset_type": request.asset_type,
            "metadata": metadata,
            "context": build_instrument_context(canonical, request.asset_type, metadata),
            "source": "yfinance" if metadata else "symbol_utils",
        }
        lessons = TradingMemoryLog(self._server_config).get_past_context(
            canonical, as_of=resolved_date
        )
        frozen_config = {
            key: deepcopy(self._server_config[key])
            for key in DATA_CONFIG_KEYS
            if key in self._server_config
        }
        frozen_config.update(
            output_language=request.output_language,
            max_debate_rounds=request.research_rounds,
            max_risk_discuss_rounds=request.risk_rounds,
        )
        frozen_config.setdefault("data_vendors", {}).update(overrides["categories"])
        frozen_config.setdefault("tool_vendors", {}).update(overrides["tools"])

        record, _ = self._store.create_run(
            request_id=request.request_id,
            request_hash=digest_json(fingerprint),
            normalized_inputs=normalized_inputs,
            current_stage=f"analyst/{request.analysts[0]}",
            frozen_config=frozen_config,
            instrument=instrument,
            lessons=lessons,
            now=datetime.now(timezone.utc).isoformat(),
        )
        return self._analysis_result(record)

    def get_analysis(
        self,
        run_id: str,
        section: str | None = None,
        evidence_id: str | None = None,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> AnalysisResult:
        snapshot = self._store.get_snapshot(run_id)
        if section is not None and evidence_id is not None:
            raise ValueError("section and evidence_id are mutually exclusive")
        if evidence_id is None and cursor is not None:
            raise ValueError("cursor requires evidence_id")
        if evidence_id is None and page_size is not None:
            raise ValueError("page_size requires evidence_id")

        _require_compatible_run(snapshot.run)

        if section is not None:
            selected = next((item for item in snapshot.outputs if item.stage_id == section), None)
            if selected is None:
                raise RunNotFound(f"RUN_NOT_FOUND: section {section} does not exist in {run_id}")
            return self._analysis_result(snapshot, selected_section=selected)
        if evidence_id is not None:
            evidence = next(
                (item for item in snapshot.evidence if item.evidence_id == evidence_id), None
            )
            if evidence is None:
                raise RunNotFound(
                    f"RUN_NOT_FOUND: evidence {evidence_id} does not exist in {run_id}"
                )
            return self._analysis_result(
                snapshot,
                evidence_page=_page_record(
                    evidence, cursor=cursor, page_size=page_size, reused=True
                ),
            )

        return self._analysis_result(snapshot, stage_view=describe_stage(snapshot))

    def submit_stage(
        self,
        run_id: str,
        stage_id: str,
        expected_revision: int,
        output: str | dict,
    ) -> SubmissionResult:
        return self._submission_result(
            self._workflow.submit_stage(run_id, stage_id, expected_revision, output)
        )

    def list_analyses(
        self,
        ticker: str | None = None,
        status: Literal["active", "ready_to_finalize", "completed", "cancelled"] | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> AnalysisListResult:
        return self._list_result(ticker, status, limit, cursor)

    def cancel_analysis(
        self,
        run_id: str,
        expected_revision: int,
    ) -> CancellationResult:
        return self._cancellation_result(
            self._workflow.cancel_analysis(run_id, expected_revision)
        )

    def _analysis_result(
        self,
        snapshot: AnalysisSnapshot | RunRecord,
        *,
        stage_view=None,
        selected_section: StageOutputRecord | None = None,
        evidence_page: DataToolResult | None = None,
    ) -> AnalysisResult:
        if isinstance(snapshot, RunRecord):
            snapshot = self._store.get_snapshot(snapshot.run_id)
        record = snapshot.run
        inputs = record.normalized_inputs
        evidence = [
            EvidenceMetadata(
                evidence_id=item.evidence_id,
                stage_id=item.stage_id,
                tool_name=item.tool_name,
                status=item.status,
                fetched_at=item.fetched_at,
                source=item.source,
                warnings=item.warnings,
            )
            for item in snapshot.evidence
        ]
        sections = [self._stage_output_result(item) for item in snapshot.outputs]
        return AnalysisResult(
            run_id=record.run_id,
            request_id=record.request_id,
            status=record.status,
            revision=record.revision,
            current_stage=record.current_stage,
            analysis_date=inputs["analysis_date"],
            analysts=inputs["analysts"],
            research_rounds=inputs["research_rounds"],
            risk_rounds=inputs["risk_rounds"],
            output_language=inputs["output_language"],
            frozen_config=record.frozen_config,
            instrument=record.instrument,
            lessons=record.lessons,
            evidence=evidence,
            active_role=None if stage_view is None else stage_view.active_role,
            instructions=None if stage_view is None else stage_view.instructions,
            required_evidence=(
                []
                if stage_view is None
                else [EvidenceCheckResult(**vars(item)) for item in stage_view.required_evidence]
            ),
            output_schema=None if stage_view is None else stage_view.output_schema,
            sections=sections,
            selected_section=(
                None if selected_section is None else self._stage_output_result(selected_section)
            ),
            evidence_page=evidence_page,
        )

    @staticmethod
    def _stage_output_result(record: StageOutputRecord) -> StageOutputResult:
        return StageOutputResult(
            receipt_id=record.receipt_id,
            stage_id=record.stage_id,
            role_key=record.role_key,
            output_kind=record.output_kind,
            canonical_output=record.canonical_output,
            rendered_output=record.rendered_output,
            accepted_revision=record.accepted_revision,
            accepted_at=record.accepted_at,
        )

    def _submission_result(self, accepted) -> SubmissionResult:
        return SubmissionResult(
            run_id=accepted.run_id,
            status=accepted.status,
            revision=accepted.revision,
            current_stage=accepted.current_stage,
            receipt=self._stage_output_result(accepted.receipt),
        )

    def _list_result(self, ticker, status, limit, cursor) -> AnalysisListResult:
        limit = _validate_limit(limit, "limit")
        ticker = None if ticker is None else _validate_ticker(ticker)
        if status not in {None, "active", "ready_to_finalize", "completed", "cancelled"}:
            raise ValueError("invalid analysis status")
        before = None if cursor is None else _decode_analysis_cursor(cursor)
        records = self._store.list_runs(ticker, status, limit + 1, before)
        page = records[:limit]
        next_cursor = None
        if len(records) > limit:
            last = page[-1]
            next_cursor = _encode_analysis_cursor(last.created_at, last.run_id)
        return AnalysisListResult(
            analyses=[
                AnalysisSummary(
                    run_id=record.run_id,
                    ticker=record.ticker,
                    analysis_date=record.normalized_inputs["analysis_date"],
                    status=record.status,
                    revision=record.revision,
                    current_stage=record.current_stage,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
                for record in page
            ],
            next_cursor=next_cursor,
        )

    @staticmethod
    def _cancellation_result(cancelled) -> CancellationResult:
        return CancellationResult(
            run_id=cancelled.run_id,
            status=cancelled.status,
            revision=cancelled.revision,
            current_stage=cancelled.current_stage,
            changed=cancelled.changed,
        )
