from __future__ import annotations

import base64
import hashlib
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
from tradingagents.dataflows.interface import TOOLS_CATEGORIES, VENDOR_METHODS, VendorRouteResult
from tradingagents.dataflows.symbol_utils import normalize_symbol
from tradingagents.dataflows.utils import get_current_date, safe_ticker_component
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.plugin.store import EvidenceRecord, PluginStore, RunRecord

AnalystKey = Literal["market", "social", "news", "fundamentals"]
AssetType = Literal["stock", "crypto"]
MAX_ROUNDS = 10
MAX_PAGE_SIZE = 32_000
_DATA_LOCK = Lock()

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


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _encode_cursor(evidence_id: str, offset: int) -> str:
    payload = _canonical_json({"evidence_id": evidence_id, "offset": offset}).encode()
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
        return _canonical_json(content), "json"
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
    warnings = list(result.warnings)
    retryable = result.retryable

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
        elif (
            content.startswith("Error fetching")
            or "currently unavailable (network error" in content
            or content.startswith("<stocktwits unavailable")
        ):
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


def _validate_run_call(run: RunRecord, tool_name: str, arguments: dict) -> None:
    if run.status != "active":
        raise ValueError(f"analysis {run.run_id} is not active")
    if tool_name != "resolve_instrument_identity" and tool_name not in STAGE_TOOLS.get(
        run.current_stage, set()
    ):
        raise ValueError(f"tool {tool_name} is not allowed at stage {run.current_stage}")

    ticker = arguments.get("ticker")
    if ticker is not None and _validate_ticker(ticker) != run.instrument["canonical_symbol"]:
        raise ValueError("ticker does not match the analysis instrument")

    analysis_date = date.fromisoformat(run.normalized_inputs["analysis_date"])
    curr_date = arguments.get("curr_date")
    if curr_date is not None and date.fromisoformat(curr_date) != analysis_date:
        raise ValueError("curr_date must match the analysis date")

    start_value = arguments.get("start_date")
    end_value = arguments.get("end_date")
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


class PluginTools:
    def __init__(self, store: PluginStore, server_config: dict | None = None):
        self._store = store
        self._server_config = deepcopy(server_config if server_config is not None else get_config())

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
                warnings=[],
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
        argument_hash = _digest(arguments)
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
            request_hash=_digest(fingerprint),
            normalized_inputs=normalized_inputs,
            current_stage=f"analyst/{request.analysts[0]}",
            frozen_config=frozen_config,
            instrument=instrument,
            lessons=lessons,
            now=datetime.now(timezone.utc).isoformat(),
        )
        return self._analysis_result(record)

    def get_analysis(self, run_id: str) -> AnalysisResult:
        return self._analysis_result(self._store.get_run(run_id))

    def _analysis_result(self, record: RunRecord) -> AnalysisResult:
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
            for item in self._store.list_evidence(record.run_id)
        ]
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
        )
