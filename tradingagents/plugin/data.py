from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    resolve_instrument_identity,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.interface import TOOLS_CATEGORIES, VENDOR_METHODS
from tradingagents.dataflows.symbol_utils import normalize_symbol
from tradingagents.dataflows.utils import get_current_date, safe_ticker_component
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.plugin.store import PluginStore, RunRecord

AnalystKey = Literal["market", "social", "news", "fundamentals"]
AssetType = Literal["stock", "crypto"]
MAX_ROUNDS = 10
MAX_PAGE_SIZE = 32_000

DATA_CONFIG_KEYS = (
    "data_cache_dir",
    "news_article_limit",
    "global_news_article_limit",
    "global_news_lookback_days",
    "global_news_queries",
    "data_vendors",
    "tool_vendors",
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _parse_date(value: str | None) -> str:
    return date.fromisoformat(value if value is not None else get_current_date()).isoformat()


def _validate_ticker(value: str) -> str:
    trimmed = value.strip()
    safe_ticker_component(trimmed)
    return normalize_symbol(trimmed)


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
