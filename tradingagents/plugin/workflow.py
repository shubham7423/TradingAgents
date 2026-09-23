from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from tradingagents.agents.analysts.fundamentals_analyst import build_fundamentals_prompt
from tradingagents.agents.analysts.market_analyst import build_market_prompt
from tradingagents.agents.analysts.news_analyst import build_news_prompt
from tradingagents.agents.analysts.sentiment_analyst import (
    build_sentiment_prompt,
    sentiment_window_start,
)
from tradingagents.agents.managers.portfolio_manager import (
    apply_portfolio_manager_output,
    build_portfolio_manager_prompt,
)
from tradingagents.agents.managers.research_manager import (
    apply_research_manager_output,
    build_research_manager_prompt,
)
from tradingagents.agents.researchers.bear_researcher import apply_bear_output, build_bear_prompt
from tradingagents.agents.researchers.bull_researcher import apply_bull_output, build_bull_prompt
from tradingagents.agents.risk_mgmt.aggressive_debator import (
    apply_aggressive_output,
    build_aggressive_prompt,
)
from tradingagents.agents.risk_mgmt.conservative_debator import (
    apply_conservative_output,
    build_conservative_prompt,
)
from tradingagents.agents.risk_mgmt.neutral_debator import (
    apply_neutral_output,
    build_neutral_prompt,
)
from tradingagents.agents.schemas import (
    PortfolioDecision,
    ResearchPlan,
    SentimentReport,
    TraderProposal,
    render_pm_decision,
    render_research_plan,
    render_sentiment_report,
    render_trader_proposal,
)
from tradingagents.agents.trader.trader import apply_trader_output, build_trader_messages
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import get_config
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.reflection import build_reflection_messages, calculate_outcome
from tradingagents.plugin.store import (
    AnalysisSnapshot,
    EvidenceRequirement,
    IncompatibleState,
    PluginStore,
    ReflectionJobRecord,
    RunNotFound,
    RunRecord,
    StageOutputRecord,
    canonical_json,
    digest_json,
)
from tradingagents.reporting import write_report_tree

ACTIVE = "active"
READY_TO_FINALIZE = "ready_to_finalize"
SUPPORTED_STATE_SCHEMA = 1
SUPPORTED_PROMPT_SCHEMA = 1
MAX_SUBMISSION_SIZE = 32_000
MISSING_EVIDENCE = "<required evidence not yet recorded>"


@dataclass(frozen=True)
class PreparedOutput:
    role_key: str
    output_kind: Literal["text", "structured"]
    canonical_output: object
    rendered_output: str
    output_hash: str


@dataclass(frozen=True)
class EvidenceCheck:
    tool_name: str
    arguments: dict[str, object]
    satisfied: bool


@dataclass(frozen=True)
class StageView:
    active_role: str | None
    instructions: str | list[dict[str, str]] | None
    required_evidence: list[EvidenceCheck]
    output_schema: dict | None


@dataclass(frozen=True)
class AcceptedStage:
    run_id: str
    status: Literal["active", "ready_to_finalize"]
    revision: int
    current_stage: str
    receipt: StageOutputRecord


@dataclass(frozen=True)
class AcceptedReflection:
    run_id: str
    status: Literal["ready_to_finalize"]
    revision: int
    current_stage: Literal["finalize"]
    reflection: str


@dataclass(frozen=True)
class CancelledAnalysis:
    run_id: str
    status: Literal["cancelled"]
    revision: int
    current_stage: str
    changed: bool


@dataclass(frozen=True)
class AnalysisFinalization:
    run_id: str
    status: Literal["completed"]
    decision_id: str
    rating: str
    report_dir: str
    complete_report_path: str
    section_paths: list[str]
    changed: bool


@dataclass(frozen=True)
class PreparedReflections:
    jobs: list[ReflectionJobRecord]
    warnings: list[str]


@dataclass(frozen=True)
class ReflectionFinalization:
    run_id: str
    status: Literal["completed"]
    decision_id: str
    changed: bool
    warning: str | None


class StageValidationError(ValueError):
    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(f"VALIDATION_ERROR: {message}")
        self.errors = [] if errors is None else errors


class WorkflowService:
    def __init__(self, store: PluginStore, server_config: dict | None = None):
        self._store = store
        self._server_config = deepcopy(server_config if server_config is not None else get_config())

    def prepare_reflections(self, ticker: str, as_of_date: str) -> PreparedReflections:
        from datetime import date

        ticker = ticker.strip().upper()
        cutoff = date.fromisoformat(as_of_date).isoformat()
        memory = TradingMemoryLog(self._server_config)
        entries, warnings = memory.get_history(ticker)
        ambiguous = {warning.removeprefix("ambiguous legacy identity: ")
                     for warning in warnings if warning.startswith("ambiguous legacy identity:")}
        jobs = []
        for entry in entries:
            if not entry["pending"] or entry["date"] > cutoff:
                continue
            if entry["legacy_identity"] and entry["decision_id"] in ambiguous:
                continue
            outcome = calculate_outcome(
                ticker, entry["date"], self._server_config, as_of_date=cutoff
            )
            if outcome is None or outcome.resolution_date > cutoff:
                continue
            latest, _ = memory.get_history(ticker)
            current = [item for item in latest if item["decision_id"] == entry["decision_id"]]
            if len(current) != 1 or not current[0]["pending"]:
                continue
            job, _ = self._store.create_reflection_job(
                decision_id=entry["decision_id"], ticker=entry["ticker"],
                decision_date=entry["date"], rating=entry["rating"],
                decision_text=entry["decision"], raw_return=outcome.raw_return,
                alpha_return=outcome.alpha_return, holding_sessions=outcome.holding_sessions,
                benchmark=outcome.benchmark, resolution_date=outcome.resolution_date,
                now=datetime.now(timezone.utc).isoformat(),
            )
            jobs.append(job)
        return PreparedReflections(jobs, warnings)

    def describe_work(self, run_id: str):
        try:
            snapshot = self._store.get_snapshot(run_id)
            return describe_stage(snapshot)
        except RunNotFound:
            job = self._store.get_reflection_job(run_id)
            active = job.status == "active"
            return StageView(
                active_role="reflection" if active else None,
                instructions=(
                    [{"role": role, "content": content} for role, content in
                     build_reflection_messages(job.decision_text, job.raw_return,
                                               job.alpha_return, job.benchmark)]
                    if active else None
                ),
                required_evidence=[],
                output_schema={"type": "string", "minLength": 1, "maxLength": 32000}
                if active else None,
            )

    def submit_stage(
        self,
        run_id: str,
        stage_id: str,
        expected_revision: int,
        output: object,
    ) -> AcceptedStage | AcceptedReflection:
        try:
            self._store.get_snapshot(run_id)
        except RunNotFound:
            self._store.get_reflection_job(run_id)
            if stage_id != "reflection":
                raise IncompatibleState("WRONG_STAGE: reflection jobs accept only reflection") from None
            if not isinstance(output, str) or not output.strip():
                raise StageValidationError("reflection must be a nonblank string") from None
            if len(output) > MAX_SUBMISSION_SIZE:
                raise StageValidationError("reflection exceeds 32,000 characters") from None
            accepted, _ = self._store.accept_reflection(
                job_id=run_id, expected_revision=expected_revision, reflection=output,
                reflection_hash=digest_json(output), now=datetime.now(timezone.utc).isoformat(),
            )
            return AcceptedReflection(accepted.job_id, accepted.status, accepted.revision,
                                     "finalize", accepted.reflection)
        snapshot = self._store.get_snapshot(run_id)
        _require_compatible_run(snapshot.run)
        prepared = prepare_output(stage_id, output)
        required_evidence = requirements_for(snapshot.run, stage_id)
        following_stage, following_status = next_stage(snapshot.run, stage_id)
        run, receipt, _ = self._store.accept_stage(
            run_id=run_id,
            stage_id=stage_id,
            expected_revision=expected_revision,
            role_key=prepared.role_key,
            output_kind=prepared.output_kind,
            canonical_output=prepared.canonical_output,
            rendered_output=prepared.rendered_output,
            output_hash=prepared.output_hash,
            required_evidence=required_evidence,
            next_stage=following_stage,
            next_status=following_status,
            now=datetime.now(timezone.utc).isoformat(),
        )
        return AcceptedStage(
            run_id=run.run_id,
            status=run.status,
            revision=run.revision,
            current_stage=run.current_stage,
            receipt=receipt,
        )

    def cancel_analysis(self, run_id: str, expected_revision: int) -> CancelledAnalysis:
        run, changed = self._store.cancel_run(
            run_id,
            expected_revision,
            datetime.now(timezone.utc).isoformat(),
        )
        return CancelledAnalysis(
            run_id=run.run_id,
            status="cancelled",
            revision=run.revision,
            current_stage=run.current_stage,
            changed=changed,
        )

    def finalize_analysis(self, run_id: str) -> AnalysisFinalization | ReflectionFinalization:
        receipt = self._store.get_export_receipt(run_id)
        if receipt is not None:
            return AnalysisFinalization(
                run_id=receipt.run_id, status="completed", decision_id=receipt.decision_id,
                rating=receipt.rating, report_dir=receipt.report_dir,
                complete_report_path=receipt.complete_report_path,
                section_paths=receipt.section_paths, changed=False,
            )

        try:
            snapshot = self._store.get_snapshot(run_id)
        except RunNotFound:
            job = self._store.get_reflection_job(run_id)
            if job.status == "completed":
                return ReflectionFinalization(job.job_id, "completed", job.decision_id,
                                              bool(job.changed), job.warning)
            if job.status != READY_TO_FINALIZE or job.reflection is None:
                raise IncompatibleState(
                    f"RUN_NOT_READY: reflection job {run_id} is not ready"
                ) from None
            status = TradingMemoryLog(self._server_config).resolve_decision(
                decision_id=job.decision_id, ticker=job.ticker, trade_date=job.decision_date,
                raw_return=job.raw_return, alpha_return=job.alpha_return,
                holding_days=job.holding_sessions, benchmark_name=job.benchmark,
                resolution_date=job.resolution_date, reflection=job.reflection,
            )
            if status in {"missing", "ambiguous"}:
                raise IncompatibleState(
                    f"MEMORY_{status.upper()}: decision cannot be resolved"
                ) from None
            changed = status == "updated"
            warning = "ALREADY_RESOLVED" if status == "already_resolved" else None
            completed, _ = self._store.complete_reflection(
                job_id=run_id, changed=changed, warning=warning,
                now=datetime.now(timezone.utc).isoformat(),
            )
            return ReflectionFinalization(completed.job_id, "completed", completed.decision_id,
                                          bool(completed.changed), completed.warning)
        run = snapshot.run
        if run.status != READY_TO_FINALIZE:
            raise IncompatibleState(
                f"RUN_NOT_READY: analysis {run_id} has status {run.status}"
            )
        state = rebuild_role_state(snapshot)
        portfolio = next((item for item in snapshot.outputs if item.stage_id == "portfolio"), None)
        if portfolio is None or not isinstance(portfolio.canonical_output, dict):
            raise IncompatibleState("INCOMPATIBLE_STATE: portfolio decision is missing")
        rating = portfolio.canonical_output.get("rating")
        if not isinstance(rating, str) or not rating:
            raise IncompatibleState("INCOMPATIBLE_STATE: portfolio rating is invalid")

        report_dir = Path(self._server_config.get("results_dir", "results")) / "plugin" / run_id
        complete_path = write_report_tree(
            state, run.ticker, report_dir, generated_at=datetime.fromisoformat(run.created_at)
        )
        section_paths = sorted(str(path) for path in report_dir.rglob("*.md"))
        analysis_date = run.normalized_inputs["analysis_date"]
        TradingMemoryLog(self._server_config).store_decision(
            run.ticker, analysis_date, state["final_trade_decision"], decision_id=run_id
        )
        export, changed = self._store.complete_analysis_export(
            run_id=run_id, decision_id=run_id, report_dir=str(report_dir),
            complete_report_path=str(complete_path), section_paths=section_paths,
            rating=rating, now=datetime.now(timezone.utc).isoformat(),
        )
        return AnalysisFinalization(
            run_id=export.run_id, status="completed", decision_id=export.decision_id,
            rating=export.rating, report_dir=export.report_dir,
            complete_report_path=export.complete_report_path,
            section_paths=export.section_paths, changed=changed,
        )


def _require_compatible_run(run: RunRecord) -> None:
    if run.state_schema != SUPPORTED_STATE_SCHEMA:
        raise IncompatibleState(
            "INCOMPATIBLE_STATE: state schema "
            f"{run.state_schema} is not supported; expected {SUPPORTED_STATE_SCHEMA}"
        )
    if run.prompt_schema != SUPPORTED_PROMPT_SCHEMA:
        raise IncompatibleState(
            "INCOMPATIBLE_STATE: prompt schema "
            f"{run.prompt_schema} is not supported; expected {SUPPORTED_PROMPT_SCHEMA}"
        )


def role_key(stage_id: str) -> str:
    parts = stage_id.split("/")
    if len(parts) == 2 and parts[0] == "analyst" and parts[1] in ANALYST_NODE_SPECS:
        return parts[1]
    if stage_id in {"research/manager", "trader", "portfolio"}:
        return {
            "research/manager": "research_manager",
            "trader": "trader",
            "portfolio": "portfolio_manager",
        }[stage_id]
    if len(parts) == 3 and parts[0] == "research" and parts[1] in {"bull", "bear"}:
        return parts[1]
    if (
        len(parts) == 3
        and parts[0] == "risk"
        and parts[1] in {"aggressive", "conservative", "neutral"}
    ):
        return parts[1]
    raise IncompatibleState(f"INCOMPATIBLE_STATE: unknown stage {stage_id}")


_STRUCTURED_OUTPUTS = {
    "social": (SentimentReport, render_sentiment_report),
    "research_manager": (ResearchPlan, render_research_plan),
    "trader": (TraderProposal, render_trader_proposal),
    "portfolio_manager": (PortfolioDecision, render_pm_decision),
}

PROMPT_BUILDERS = {
    "market": build_market_prompt,
    "news": build_news_prompt,
    "fundamentals": build_fundamentals_prompt,
    "bull": build_bull_prompt,
    "bear": build_bear_prompt,
    "research_manager": build_research_manager_prompt,
    "trader": build_trader_messages,
    "aggressive": build_aggressive_prompt,
    "conservative": build_conservative_prompt,
    "neutral": build_neutral_prompt,
    "portfolio_manager": build_portfolio_manager_prompt,
}


def prepare_output(stage_id: str, output: object) -> PreparedOutput:
    key = role_key(stage_id)
    structured = _STRUCTURED_OUTPUTS.get(key)
    if structured is None:
        if not isinstance(output, str):
            raise StageValidationError("narrative output must be a string")
        if not output.strip():
            raise StageValidationError("narrative output must not be blank")
        canonical_output = output
        rendered_output = output
        output_kind = "text"
    else:
        if not isinstance(output, dict):
            raise StageValidationError("structured output must be an object")
        schema, renderer = structured
        try:
            validated = schema.model_validate(output)
        except ValidationError as exc:
            raise StageValidationError(
                "structured output is invalid",
                exc.errors(include_url=False),
            ) from exc
        canonical_output = validated.model_dump(mode="json")
        rendered_output = renderer(validated)
        output_kind = "structured"

    if len(canonical_json(canonical_output)) > MAX_SUBMISSION_SIZE:
        raise StageValidationError("canonical output exceeds 32,000 characters")
    return PreparedOutput(
        role_key=key,
        output_kind=output_kind,
        canonical_output=canonical_output,
        rendered_output=rendered_output,
        output_hash=digest_json(canonical_output),
    )


def requirements_for(run: RunRecord, stage_id: str) -> tuple[EvidenceRequirement, ...]:
    symbol = run.instrument["canonical_symbol"]
    end_date = run.normalized_inputs["analysis_date"]
    if stage_id == "analyst/market":
        return (EvidenceRequirement(
            "get_verified_market_snapshot",
            {"symbol": symbol, "curr_date": end_date},
        ),)
    if stage_id == "analyst/social":
        window = {
            "ticker": symbol,
            "start_date": sentiment_window_start(end_date),
            "end_date": end_date,
        }
        return tuple(
            EvidenceRequirement(tool_name, dict(window))
            for tool_name in (
                "get_news",
                "fetch_stocktwits_messages",
                "fetch_reddit_posts",
            )
        )
    return ()


def _round_number(stage_id: str, maximum: object) -> int:
    suffix = stage_id.rsplit("/", 1)[-1]
    try:
        round_number = int(suffix)
    except ValueError as exc:
        raise IncompatibleState(f"INCOMPATIBLE_STATE: unknown stage {stage_id}") from exc
    if (
        suffix != str(round_number)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not 1 <= round_number <= maximum
    ):
        raise IncompatibleState(f"INCOMPATIBLE_STATE: unknown stage {stage_id}")
    return round_number


def next_stage(run: RunRecord, stage_id: str) -> tuple[str, str]:
    _require_compatible_run(run)
    inputs = run.normalized_inputs
    analysts = inputs["analysts"]
    if stage_id.startswith("analyst/"):
        key = role_key(stage_id)
        try:
            index = analysts.index(key)
        except ValueError as exc:
            raise IncompatibleState(f"INCOMPATIBLE_STATE: unknown stage {stage_id}") from exc
        if index + 1 < len(analysts):
            return f"analyst/{analysts[index + 1]}", ACTIVE
        return "research/bull/1", ACTIVE
    if stage_id.startswith("research/bull/"):
        round_number = _round_number(stage_id, inputs["research_rounds"])
        return f"research/bear/{round_number}", ACTIVE
    if stage_id.startswith("research/bear/"):
        round_number = _round_number(stage_id, inputs["research_rounds"])
        if round_number < inputs["research_rounds"]:
            return f"research/bull/{round_number + 1}", ACTIVE
        return "research/manager", ACTIVE
    if stage_id == "research/manager":
        return "trader", ACTIVE
    if stage_id == "trader":
        return "risk/aggressive/1", ACTIVE
    if stage_id.startswith("risk/aggressive/"):
        round_number = _round_number(stage_id, inputs["risk_rounds"])
        return f"risk/conservative/{round_number}", ACTIVE
    if stage_id.startswith("risk/conservative/"):
        round_number = _round_number(stage_id, inputs["risk_rounds"])
        return f"risk/neutral/{round_number}", ACTIVE
    if stage_id.startswith("risk/neutral/"):
        round_number = _round_number(stage_id, inputs["risk_rounds"])
        if round_number < inputs["risk_rounds"]:
            return f"risk/aggressive/{round_number + 1}", ACTIVE
        return "portfolio", ACTIVE
    if stage_id == "portfolio":
        return "finalize", READY_TO_FINALIZE
    raise IncompatibleState(f"INCOMPATIBLE_STATE: unknown stage {stage_id}")


def initial_role_state(run: RunRecord) -> dict[str, object]:
    return {
        "messages": [],
        "company_of_interest": run.instrument["canonical_symbol"],
        "asset_type": run.normalized_inputs["asset_type"],
        "instrument_context": run.instrument["context"],
        "trade_date": run.normalized_inputs["analysis_date"],
        "sender": "",
        "market_report": "",
        "sentiment_report": "",
        "news_report": "",
        "fundamentals_report": "",
        "investment_debate_state": {
            "bull_history": "",
            "bear_history": "",
            "history": "",
            "current_response": "",
            "judge_decision": "",
            "count": 0,
        },
        "investment_plan": "",
        "trader_investment_plan": "",
        "risk_debate_state": {
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "history": "",
            "latest_speaker": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 0,
        },
        "final_trade_decision": "",
        "past_context": run.lessons,
    }


_OUTPUT_UPDATES: dict[str, Callable[[Mapping[str, Any], str], dict[str, Any]]] = {
    "bull": apply_bull_output,
    "bear": apply_bear_output,
    "research_manager": apply_research_manager_output,
    "trader": apply_trader_output,
    "aggressive": apply_aggressive_output,
    "conservative": apply_conservative_output,
    "neutral": apply_neutral_output,
    "portfolio_manager": apply_portfolio_manager_output,
}


def rebuild_role_state(snapshot: AnalysisSnapshot) -> dict[str, object]:
    _require_compatible_run(snapshot.run)
    state = initial_role_state(snapshot.run)
    analysts = snapshot.run.normalized_inputs["analysts"]
    if not analysts:
        raise IncompatibleState("INCOMPATIBLE_STATE: run has no analysts")
    expected_stage = f"analyst/{analysts[0]}"

    for output in snapshot.outputs:
        if output.stage_id != expected_stage:
            raise IncompatibleState(
                f"INCOMPATIBLE_STATE: expected stage {expected_stage}, got {output.stage_id}"
            )
        key = role_key(output.stage_id)
        if output.stage_id.startswith("analyst/"):
            state[ANALYST_NODE_SPECS[key].report_key] = output.rendered_output
        else:
            state.update(_OUTPUT_UPDATES[key](state, output.rendered_output))
        expected_stage, _ = next_stage(snapshot.run, output.stage_id)

    return state


def _matching_evidence(snapshot: AnalysisSnapshot, requirement: EvidenceRequirement):
    for record in snapshot.evidence:
        if record.stage_id != snapshot.run.current_stage or record.tool_name != requirement.tool_name:
            continue
        if all(
            name in record.arguments
            and canonical_json(record.arguments[name]) == canonical_json(value)
            for name, value in requirement.arguments.items()
        ):
            return record
    return None


def describe_stage(snapshot: AnalysisSnapshot) -> StageView:
    run = snapshot.run
    _require_compatible_run(run)
    if run.status != ACTIVE:
        return StageView(None, None, [], None)

    stage_id = run.current_stage
    key = role_key(stage_id)
    requirements = requirements_for(run, stage_id)
    matches = [_matching_evidence(snapshot, requirement) for requirement in requirements]
    checks = [
        EvidenceCheck(requirement.tool_name, requirement.arguments, record is not None)
        for requirement, record in zip(requirements, matches, strict=True)
    ]
    state = rebuild_role_state(snapshot)
    language = run.normalized_inputs["output_language"]
    if key == "social":
        blocks = [MISSING_EVIDENCE if record is None else record.content for record in matches]
        instructions = build_sentiment_prompt(
            state,
            output_language=language,
            news_block=blocks[0],
            stocktwits_block=blocks[1],
            reddit_block=blocks[2],
        )
    else:
        instructions = PROMPT_BUILDERS[key](state, output_language=language)
    structured = _STRUCTURED_OUTPUTS.get(key)
    return StageView(
        active_role=key,
        instructions=instructions,
        required_evidence=checks,
        output_schema=None if structured is None else structured[0].model_json_schema(),
    )
