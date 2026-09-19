import pytest

from tradingagents.plugin.store import (
    AnalysisSnapshot,
    IncompatibleState,
    RunRecord,
    StageOutputRecord,
)
from tradingagents.plugin.workflow import initial_role_state, next_stage, rebuild_role_state


def run_record(**overrides):
    normalized_inputs = {
        "ticker": "AAPL",
        "asset_type": "stock",
        "analysis_date": "2026-09-15",
        "analysts": ["market"],
        "research_rounds": 1,
        "risk_rounds": 1,
        "output_language": "French",
    }
    normalized_inputs.update(overrides)
    return RunRecord(
        run_id="run-1",
        request_id="request-1",
        request_hash="hash-1",
        normalized_inputs=normalized_inputs,
        ticker="AAPL",
        status="active",
        revision=1,
        current_stage=f"analyst/{normalized_inputs['analysts'][0]}",
        frozen_config={"output_language": normalized_inputs["output_language"]},
        instrument={
            "canonical_symbol": "AAPL",
            "context": "Apple Inc. (AAPL, NASDAQ, USD)",
        },
        lessons="PAST LESSONS",
        state_schema=1,
        prompt_schema=1,
        created_at="2026-09-15T12:00:00+00:00",
        updated_at="2026-09-15T12:00:00+00:00",
    )


def stage_output(stage_id, rendered_output, revision):
    stored_role = {
        "research/manager": "research_manager",
        "trader": "trader",
        "portfolio": "portfolio_manager",
    }.get(stage_id)
    if stored_role is None:
        stored_role = stage_id.split("/")[1]
    return StageOutputRecord(
        receipt_id=f"receipt-{revision}",
        run_id="run-1",
        stage_id=stage_id,
        role_key=stored_role,
        output_kind="text",
        canonical_output=rendered_output,
        rendered_output=rendered_output,
        output_hash=f"hash-{revision}",
        accepted_revision=revision,
        accepted_at=f"2026-09-15T12:{revision:02d}:00+00:00",
    )


def snapshot_with_outputs():
    run = run_record()
    outputs = [
        stage_output("analyst/market", "MARKET", 1),
        stage_output("research/bull/1", "BULL", 2),
        stage_output("research/bear/1", "BEAR", 3),
        stage_output("research/manager", "RESEARCH PLAN", 4),
        stage_output("trader", "TRADER", 5),
        stage_output("risk/aggressive/1", "AGGRESSIVE", 6),
        stage_output("risk/conservative/1", "CONSERVATIVE", 7),
        stage_output("risk/neutral/1", "NEUTRAL", 8),
    ]
    return AnalysisSnapshot(run=run, outputs=outputs, evidence=[])


def test_selected_analysts_and_rounds_define_exact_sequence():
    run = run_record(
        analysts=["news", "market"],
        research_rounds=2,
        risk_rounds=2,
    )
    stages = ["analyst/news"]
    statuses = []
    while stages[-1] != "finalize":
        stage, status = next_stage(run, stages[-1])
        stages.append(stage)
        statuses.append(status)

    assert stages == [
        "analyst/news",
        "analyst/market",
        "research/bull/1",
        "research/bear/1",
        "research/bull/2",
        "research/bear/2",
        "research/manager",
        "trader",
        "risk/aggressive/1",
        "risk/conservative/1",
        "risk/neutral/1",
        "risk/aggressive/2",
        "risk/conservative/2",
        "risk/neutral/2",
        "portfolio",
        "finalize",
    ]
    assert statuses[-1] == "ready_to_finalize"
    assert all(status == "active" for status in statuses[:-1])


@pytest.mark.parametrize(
    "stage_id",
    [
        "analyst/news",
        "research/bull/not-a-round",
        "research/bull/01",
        "research/bull/0",
        "research/bear/2",
        "risk/aggressive/not-a-round",
        "risk/neutral/+1",
        "risk/conservative/0",
        "risk/neutral/2",
        "finalize",
        "unknown",
    ],
)
def test_invalid_or_incompatible_stage_is_rejected(stage_id):
    with pytest.raises(IncompatibleState, match="INCOMPATIBLE_STATE"):
        next_stage(run_record(), stage_id)


def test_initial_role_state_contains_frozen_inputs_and_empty_workflow_state():
    run = run_record()

    state = initial_role_state(run)

    assert state["company_of_interest"] == "AAPL"
    assert state["asset_type"] == "stock"
    assert state["instrument_context"] == "Apple Inc. (AAPL, NASDAQ, USD)"
    assert state["trade_date"] == "2026-09-15"
    assert run.normalized_inputs["output_language"] == "French"
    assert state["past_context"] == "PAST LESSONS"
    assert state["market_report"] == ""
    assert state["sentiment_report"] == ""
    assert state["news_report"] == ""
    assert state["fundamentals_report"] == ""
    assert state["investment_debate_state"]["history"] == ""
    assert state["investment_debate_state"]["count"] == 0
    assert state["risk_debate_state"]["history"] == ""
    assert state["risk_debate_state"]["count"] == 0


def test_rebuild_role_state_uses_rendered_outputs_and_shared_updates():
    state = rebuild_role_state(snapshot_with_outputs())

    assert state["market_report"] == "MARKET"
    assert state["investment_debate_state"]["history"] == (
        "\nBull Analyst: BULL\nBear Analyst: BEAR"
    )
    assert state["investment_debate_state"]["count"] == 2
    assert state["investment_plan"] == "RESEARCH PLAN"
    assert state["trader_investment_plan"] == "TRADER"
    assert state["risk_debate_state"]["count"] == 3


def test_rebuild_role_state_rejects_noncontiguous_output_sequence():
    snapshot = snapshot_with_outputs()
    snapshot.outputs[1] = stage_output("research/bear/1", "BEAR", 2)

    with pytest.raises(IncompatibleState, match="INCOMPATIBLE_STATE"):
        rebuild_role_state(snapshot)
