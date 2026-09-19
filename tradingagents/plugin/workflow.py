from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.plugin.store import (
    AnalysisSnapshot,
    EvidenceRequirement,
    IncompatibleState,
    RunRecord,
    canonical_json,
    digest_json,
)

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


class StageValidationError(ValueError):
    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(f"VALIDATION_ERROR: {message}")
        self.errors = [] if errors is None else errors


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
