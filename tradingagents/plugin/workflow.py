from collections.abc import Callable, Mapping
from typing import Any

from tradingagents.agents.managers.portfolio_manager import apply_portfolio_manager_output
from tradingagents.agents.managers.research_manager import apply_research_manager_output
from tradingagents.agents.researchers.bear_researcher import apply_bear_output
from tradingagents.agents.researchers.bull_researcher import apply_bull_output
from tradingagents.agents.risk_mgmt.aggressive_debator import apply_aggressive_output
from tradingagents.agents.risk_mgmt.conservative_debator import apply_conservative_output
from tradingagents.agents.risk_mgmt.neutral_debator import apply_neutral_output
from tradingagents.agents.trader.trader import apply_trader_output
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.plugin.store import AnalysisSnapshot, IncompatibleState, RunRecord

ACTIVE = "active"
READY_TO_FINALIZE = "ready_to_finalize"
SUPPORTED_STATE_SCHEMA = 1
SUPPORTED_PROMPT_SCHEMA = 1


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
