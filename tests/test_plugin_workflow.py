import sqlite3

import pytest

from tradingagents.agents.analysts.fundamentals_analyst import build_fundamentals_prompt
from tradingagents.agents.analysts.market_analyst import build_market_prompt
from tradingagents.agents.analysts.news_analyst import build_news_prompt
from tradingagents.agents.managers.portfolio_manager import build_portfolio_manager_prompt
from tradingagents.agents.schemas import (
    PortfolioDecision,
    ResearchPlan,
    SentimentReport,
    TraderProposal,
)
from tradingagents.agents.trader.trader import build_trader_messages
from tradingagents.plugin.store import (
    AnalysisSnapshot,
    EvidenceRecord,
    EvidenceRequirement,
    IncompatibleState,
    MissingEvidence,
    PluginStore,
    RunRecord,
    StageAlreadyAccepted,
    StageOutputRecord,
    StaleRevision,
    WrongStage,
    digest_json,
)
from tradingagents.plugin.workflow import (
    CancelledAnalysis,
    StageValidationError,
    WorkflowService,
    describe_stage,
    initial_role_state,
    next_stage,
    prepare_output,
    rebuild_role_state,
    requirements_for,
)


@pytest.fixture
def store(tmp_path):
    return PluginStore(tmp_path)


def create_run(
    store,
    *,
    analysts=None,
    stage=None,
    status="active",
    research_rounds=1,
    risk_rounds=1,
    output_language="English",
):
    analysts = analysts or ["market"]
    normalized_inputs = {
        "ticker": "AAPL",
        "asset_type": "stock",
        "analysis_date": "2026-09-15",
        "analysts": analysts,
        "research_rounds": research_rounds,
        "risk_rounds": risk_rounds,
        "output_language": output_language,
    }
    run, _ = store.create_run(
        request_id=f"request-{stage or analysts[0]}",
        request_hash="request-hash",
        normalized_inputs=normalized_inputs,
        current_stage=stage or f"analyst/{analysts[0]}",
        frozen_config={"output_language": output_language},
        instrument={
            "requested_symbol": "AAPL",
            "canonical_symbol": "AAPL",
            "context": "Apple Inc. (AAPL, NASDAQ, USD)",
        },
        lessons="",
        now="2026-09-15T12:00:00+00:00",
    )
    if status != "active":
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?", (status, run.run_id)
            )
        run = store.get_run(run.run_id)
    return run


def save_required_market_snapshot(store, run):
    store.save_evidence(
        run_id=run.run_id,
        expected_stage="analyst/market",
        tool_name="get_verified_market_snapshot",
        argument_hash="market-snapshot",
        arguments={"symbol": "AAPL", "curr_date": "2026-09-15"},
        status="success",
        fetched_at="2026-09-15T12:01:00+00:00",
        requested_window={},
        content="market snapshot",
        content_format="text",
        source={},
        warnings=[],
    )


def save_required_social_evidence(store, run):
    arguments = {
        "ticker": "AAPL",
        "start_date": "2026-09-08",
        "end_date": "2026-09-15",
    }
    for tool_name in ("get_news", "fetch_stocktwits_messages", "fetch_reddit_posts"):
        store.save_evidence(
            run_id=run.run_id,
            expected_stage="analyst/social",
            tool_name=tool_name,
            argument_hash=tool_name,
            arguments=arguments,
            status="success",
            fetched_at="2026-09-15T12:01:00+00:00",
            requested_window={},
            content=tool_name,
            content_format="text",
            source={},
            warnings=[],
        )


def valid_stage_output(stage_id):
    role = stage_id.split("/")[1] if "/" in stage_id else stage_id
    return {
        "social": {
            "overall_band": "Mixed",
            "overall_score": 5.0,
            "confidence": "medium",
            "narrative": "Saved sources are mixed.",
        },
        "manager": {
            "recommendation": "Hold",
            "rationale": "The debate is balanced.",
            "strategic_actions": "Maintain exposure.",
        },
        "trader": {"action": "Hold", "reasoning": "Evidence remains balanced."},
        "portfolio": {
            "rating": "Hold",
            "executive_summary": "Maintain the position.",
            "investment_thesis": "The risk-adjusted outlook is balanced.",
        },
    }.get(role, f"{stage_id} saved output")


def run_record(*, state_schema=1, prompt_schema=1, stage=None, status="active", **overrides):
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
        status=status,
        revision=1,
        current_stage=stage or f"analyst/{normalized_inputs['analysts'][0]}",
        frozen_config={"output_language": normalized_inputs["output_language"]},
        instrument={
            "canonical_symbol": "AAPL",
            "context": "Apple Inc. (AAPL, NASDAQ, USD)",
        },
        lessons="PAST LESSONS",
        state_schema=state_schema,
        prompt_schema=prompt_schema,
        created_at="2026-09-15T12:00:00+00:00",
        updated_at="2026-09-15T12:00:00+00:00",
    )


def evidence_record(stage_id, tool_name, arguments, content):
    return EvidenceRecord(
        evidence_id=f"evidence-{tool_name}",
        run_id="run-1",
        stage_id=stage_id,
        tool_name=tool_name,
        argument_hash="argument-hash",
        arguments=arguments,
        status="success",
        fetched_at="2026-09-17T12:00:00+00:00",
        requested_window={},
        content=content,
        content_format="text",
        source={},
        warnings=[],
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


@pytest.mark.parametrize(
    ("schema_overrides", "message"),
    [
        ({"state_schema": 2}, "state schema 2 is not supported; expected 1"),
        ({"prompt_schema": 2}, "prompt schema 2 is not supported; expected 1"),
    ],
)
def test_next_stage_rejects_unsupported_run_schema(schema_overrides, message):
    with pytest.raises(IncompatibleState, match=message):
        next_stage(run_record(**schema_overrides), "analyst/market")


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


@pytest.mark.parametrize(
    ("schema_overrides", "message"),
    [
        ({"state_schema": 2}, "state schema 2 is not supported; expected 1"),
        ({"prompt_schema": 2}, "prompt schema 2 is not supported; expected 1"),
    ],
)
def test_rebuild_role_state_rejects_unsupported_run_schema(schema_overrides, message):
    valid_snapshot = snapshot_with_outputs()
    snapshot = AnalysisSnapshot(
        run=run_record(**schema_overrides),
        outputs=valid_snapshot.outputs,
        evidence=[],
    )

    with pytest.raises(IncompatibleState, match=message):
        rebuild_role_state(snapshot)


def test_prepare_output_preserves_text_and_normalizes_structured_payloads():
    text = prepare_output("research/bull/1", "  bullish case  ")
    plan = prepare_output("research/manager", {
        "recommendation": "Hold",
        "rationale": "Evidence is balanced.",
        "strategic_actions": "Keep current exposure.",
    })

    assert text.output_kind == "text"
    assert text.canonical_output == "  bullish case  "
    assert text.rendered_output == "  bullish case  "
    assert plan.output_kind == "structured"
    assert plan.canonical_output["recommendation"] == "Hold"
    assert plan.rendered_output.startswith("**Recommendation**: Hold")
    assert plan.output_hash == digest_json(plan.canonical_output)


def test_prepare_output_returns_field_errors_without_freetext_fallback():
    with pytest.raises(StageValidationError) as error:
        prepare_output("trader", {"action": "Maybe", "reasoning": "unclear"})

    assert error.value.errors[0]["loc"] == ("action",)


@pytest.mark.parametrize("output", ["", " \n\t "])
def test_prepare_output_rejects_blank_narrative(output):
    with pytest.raises(StageValidationError, match="VALIDATION_ERROR"):
        prepare_output("research/bull/1", output)


@pytest.mark.parametrize(
    ("stage_id", "output"),
    [
        ("research/bull/1", {"argument": "bullish"}),
        ("research/manager", "Hold because evidence is balanced."),
    ],
)
def test_prepare_output_rejects_wrong_native_kind(stage_id, output):
    with pytest.raises(StageValidationError, match="VALIDATION_ERROR"):
        prepare_output(stage_id, output)


@pytest.mark.parametrize(
    ("stage_id", "payload", "schema"),
    [
        (
            "analyst/social",
            {
                "overall_band": "Mixed",
                "overall_score": 5.0,
                "confidence": "medium",
                "narrative": "Sources disagree.",
            },
            SentimentReport,
        ),
        (
            "research/manager",
            {
                "recommendation": "Hold",
                "rationale": "Balanced evidence.",
                "strategic_actions": "Maintain exposure.",
            },
            ResearchPlan,
        ),
        (
            "trader",
            {"action": "Buy", "reasoning": "Evidence supports entry."},
            TraderProposal,
        ),
        (
            "portfolio",
            {
                "rating": "Overweight",
                "executive_summary": "Add gradually.",
                "investment_thesis": "Risk-adjusted upside is favorable.",
            },
            PortfolioDecision,
        ),
    ],
)
def test_prepare_output_uses_each_native_structured_schema(stage_id, payload, schema):
    prepared = prepare_output(stage_id, payload)

    assert prepared.output_kind == "structured"
    assert schema.model_validate(prepared.canonical_output)
    assert prepared.rendered_output


def test_prepare_output_rejects_canonical_payload_over_32000_characters():
    with pytest.raises(StageValidationError, match="32,000"):
        prepare_output("research/bull/1", "x" * 32_001)


def test_market_and_sentiment_require_exact_recorded_attempts():
    market = requirements_for(
        run_record(stage="analyst/market", analysis_date="2026-09-17"),
        "analyst/market",
    )
    social = requirements_for(
        run_record(stage="analyst/social", analysis_date="2026-09-17"),
        "analyst/social",
    )

    assert market == (EvidenceRequirement(
        "get_verified_market_snapshot",
        {"symbol": "AAPL", "curr_date": "2026-09-17"},
    ),)
    assert [item.tool_name for item in social] == [
        "get_news", "fetch_stocktwits_messages", "fetch_reddit_posts",
    ]
    assert all(item.arguments["start_date"] == "2026-09-10" for item in social)
    assert all(item.arguments["end_date"] == "2026-09-17" for item in social)
    assert requirements_for(run_record(), "analyst/news") == ()


@pytest.mark.parametrize(
    ("stage_id", "builder"),
    [
        ("analyst/market", build_market_prompt),
        ("analyst/news", build_news_prompt),
        ("analyst/fundamentals", build_fundamentals_prompt),
    ],
)
def test_describe_stage_uses_shared_analyst_prompt_builders(stage_id, builder):
    run = run_record(stage=stage_id, analysts=[stage_id.split("/")[1]])
    snapshot = AnalysisSnapshot(run=run, outputs=[], evidence=[])

    view = describe_stage(snapshot)

    assert view.instructions == builder(initial_role_state(run), output_language="French")


def test_describe_sentiment_uses_saved_evidence_and_marks_missing_sources():
    run = run_record(
        stage="analyst/social",
        analysts=["social"],
        analysis_date="2026-09-17",
    )
    evidence = [
        evidence_record(
            "analyst/social",
            "get_news",
            {
                "ticker": "AAPL",
                "start_date": "2026-09-10",
                "end_date": "2026-09-17",
                "limit": 10,
            },
            "SAVED NEWS",
        ),
        evidence_record(
            "analyst/social",
            "fetch_stocktwits_messages",
            {
                "ticker": "AAPL",
                "start_date": "2026-09-10",
                "end_date": "2026-09-17",
                "limit": 30,
            },
            "SAVED STOCKTWITS",
        ),
        evidence_record(
            "analyst/news",
            "fetch_reddit_posts",
            {
                "ticker": "AAPL",
                "start_date": "2026-09-10",
                "end_date": "2026-09-17",
            },
            "WRONG-STAGE REDDIT",
        ),
    ]

    view = describe_stage(AnalysisSnapshot(run=run, outputs=[], evidence=evidence))

    assert "SAVED NEWS" in view.instructions
    assert "SAVED STOCKTWITS" in view.instructions
    assert "<required evidence not yet recorded>" in view.instructions
    assert "WRONG-STAGE REDDIT" not in view.instructions
    assert [check.satisfied for check in view.required_evidence] == [True, True, False]
    assert view.output_schema == SentimentReport.model_json_schema()


def test_describe_stage_uses_rebuilt_state_and_frozen_role_contracts():
    opening_run = run_record(stage="research/bull/1")
    opening = describe_stage(AnalysisSnapshot(run=opening_run, outputs=[], evidence=[]))
    assert "bear analyst has not spoken yet" in opening.instructions

    trader_snapshot = snapshot_with_outputs()
    trader_snapshot = AnalysisSnapshot(
        run=run_record(stage="trader"),
        outputs=trader_snapshot.outputs[:4],
        evidence=[],
    )
    trader = describe_stage(trader_snapshot)
    trader_state = rebuild_role_state(trader_snapshot)
    assert trader.instructions == build_trader_messages(trader_state, output_language="French")
    assert trader.output_schema == TraderProposal.model_json_schema()

    portfolio_snapshot = snapshot_with_outputs()
    portfolio_snapshot = AnalysisSnapshot(
        run=run_record(stage="portfolio"),
        outputs=portfolio_snapshot.outputs,
        evidence=[],
    )
    portfolio = describe_stage(portfolio_snapshot)
    portfolio_state = rebuild_role_state(portfolio_snapshot)
    assert portfolio.instructions == build_portfolio_manager_prompt(
        portfolio_state, output_language="French"
    )
    assert "PAST LESSONS" in portfolio.instructions
    assert portfolio.output_schema == PortfolioDecision.model_json_schema()


@pytest.mark.parametrize("status", ["ready_to_finalize", "cancelled", "completed"])
def test_describe_stage_hides_inactive_runs(status):
    view = describe_stage(AnalysisSnapshot(run=run_record(status=status), outputs=[], evidence=[]))

    assert view.active_role is None
    assert view.instructions is None
    assert view.required_evidence == []
    assert view.output_schema is None


def test_service_submits_selected_analyst_and_returns_next_stage(store):
    run = create_run(store, analysts=["market"])
    save_required_market_snapshot(store, run)
    service = WorkflowService(store)

    result = service.submit_stage(run.run_id, "analyst/market", 1, "Market report")

    assert result.receipt.stage_id == "analyst/market"
    assert result.revision == 2
    assert result.current_stage == "research/bull/1"
    assert result.status == "active"


def test_service_structured_field_error_does_not_advance(store):
    run = create_run(store, stage="trader")

    with pytest.raises(StageValidationError) as error:
        WorkflowService(store).submit_stage(
            run.run_id,
            "trader",
            1,
            {"action": "Maybe", "reasoning": "unclear"},
        )

    assert error.value.errors[0]["loc"] == ("action",)
    assert store.get_run(run.run_id) == run
    assert store.get_snapshot(run.run_id).outputs == []


def test_service_missing_evidence_does_not_advance(store):
    run = create_run(store, analysts=["market"])

    with pytest.raises(MissingEvidence, match="^MISSING_EVIDENCE:"):
        WorkflowService(store).submit_stage(
            run.run_id, "analyst/market", 1, "Market report"
        )

    assert store.get_run(run.run_id) == run
    assert store.get_snapshot(run.run_id).outputs == []


def test_service_canonical_retry_returns_original_receipt(store):
    run = create_run(store, analysts=["social"])
    save_required_social_evidence(store, run)
    service = WorkflowService(store)
    first_payload = {
        "overall_band": "Mixed",
        "overall_score": 5.0,
        "confidence": "medium",
        "narrative": "Sources disagree.",
    }

    first = service.submit_stage(run.run_id, "analyst/social", 1, first_payload)
    retried = service.submit_stage(
        run.run_id,
        "analyst/social",
        1,
        dict(reversed(list(first_payload.items()))),
    )

    assert retried == first


def test_service_conflicting_retry_is_rejected(store):
    run = create_run(store, analysts=["market"])
    save_required_market_snapshot(store, run)
    service = WorkflowService(store)
    accepted = service.submit_stage(run.run_id, "analyst/market", 1, "Market report")

    with pytest.raises(StageAlreadyAccepted, match="^STAGE_ALREADY_ACCEPTED:"):
        service.submit_stage(run.run_id, "analyst/market", 1, "Different report")

    assert store.get_run(run.run_id).revision == accepted.revision


def test_service_rejects_stale_revision_and_out_of_order_stage(store):
    run = create_run(store, analysts=["market"])
    save_required_market_snapshot(store, run)
    service = WorkflowService(store)

    with pytest.raises(StaleRevision, match="^STALE_REVISION:"):
        service.submit_stage(run.run_id, "analyst/market", 2, "Market report")
    with pytest.raises(WrongStage, match="^WRONG_STAGE:"):
        service.submit_stage(run.run_id, "risk/aggressive/1", 1, "Risk report")

    assert store.get_run(run.run_id) == run


def test_service_final_portfolio_submission_is_ready_to_finalize(store):
    run = create_run(store, stage="portfolio")
    payload = {
        "rating": "Overweight",
        "executive_summary": "Add gradually.",
        "investment_thesis": "Risk-adjusted upside is favorable.",
    }

    result = WorkflowService(store).submit_stage(run.run_id, "portfolio", 1, payload)

    assert result.status == "ready_to_finalize"
    assert result.current_stage == "finalize"
    assert result.revision == 2


@pytest.mark.parametrize("status", ["active", "ready_to_finalize"])
def test_service_cancellation_is_idempotent_and_reports_change(store, status):
    run = create_run(store, stage="portfolio", status=status)
    service = WorkflowService(store)

    cancelled = service.cancel_analysis(run.run_id, 1)
    repeated = service.cancel_analysis(run.run_id, 999)

    assert cancelled.status == "cancelled"
    assert cancelled.revision == 2
    assert cancelled.current_stage == "portfolio"
    assert cancelled.changed is True
    assert repeated == CancelledAnalysis(
        run_id=run.run_id,
        status="cancelled",
        revision=2,
        current_stage="portfolio",
        changed=False,
    )


def test_complete_multi_round_run_survives_restarts(tmp_path, monkeypatch):
    from tradingagents import llm_clients

    def reject_llm_client(*args, **kwargs):
        raise AssertionError("the plugin workflow must not construct an LLM client")

    monkeypatch.setattr(llm_clients, "create_llm_client", reject_llm_client)
    root = tmp_path / "state"
    store = PluginStore(root)
    run = create_run(
        store,
        analysts=["market", "social"],
        research_rounds=2,
        risk_rounds=2,
        output_language="French",
    )
    save_required_market_snapshot(store, run)
    service = WorkflowService(store)
    accepted = service.submit_stage(run.run_id, "analyst/market", 1, "MARKET SAVED")

    reopened = PluginStore(root)
    save_required_social_evidence(reopened, reopened.get_run(run.run_id))
    analyst_view = describe_stage(reopened.get_snapshot(run.run_id))
    assert analyst_view.active_role == "social"
    assert all(check.satisfied for check in analyst_view.required_evidence)
    assert "get_news" in analyst_view.instructions
    assert "French" in analyst_view.instructions

    service = WorkflowService(reopened)
    accepted = service.submit_stage(
        run.run_id,
        accepted.current_stage,
        accepted.revision,
        valid_stage_output(accepted.current_stage),
    )
    accepted = service.submit_stage(
        run.run_id,
        accepted.current_stage,
        accepted.revision,
        valid_stage_output(accepted.current_stage),
    )

    reopened = PluginStore(root)
    research_view = describe_stage(reopened.get_snapshot(run.run_id))
    assert research_view.active_role == "bear"
    assert "research/bull/1 saved output" in research_view.instructions
    assert "French" in research_view.instructions

    service = WorkflowService(reopened)
    while not accepted.current_stage.startswith("risk/"):
        accepted = service.submit_stage(
            run.run_id,
            accepted.current_stage,
            accepted.revision,
            valid_stage_output(accepted.current_stage),
        )
    accepted = service.submit_stage(
        run.run_id,
        accepted.current_stage,
        accepted.revision,
        valid_stage_output(accepted.current_stage),
    )

    reopened = PluginStore(root)
    risk_view = describe_stage(reopened.get_snapshot(run.run_id))
    assert risk_view.active_role == "conservative"
    assert "risk/aggressive/1 saved output" in risk_view.instructions
    assert "French" in risk_view.instructions

    service = WorkflowService(reopened)
    while accepted.current_stage != "finalize":
        accepted = service.submit_stage(
            run.run_id,
            accepted.current_stage,
            accepted.revision,
            valid_stage_output(accepted.current_stage),
        )

    final = reopened.get_snapshot(run.run_id)
    research = [item for item in final.outputs if item.stage_id.startswith("research/")]
    risk = [item for item in final.outputs if item.stage_id.startswith("risk/")]

    assert [item.role_key for item in research[:-1]] == ["bull", "bear", "bull", "bear"]
    assert [item.role_key for item in risk] == [
        "aggressive", "conservative", "neutral",
        "aggressive", "conservative", "neutral",
    ]
    assert final.run.status == "ready_to_finalize"
    assert final.run.current_stage == "finalize"
    assert final.run.revision == 1 + len(final.outputs)
