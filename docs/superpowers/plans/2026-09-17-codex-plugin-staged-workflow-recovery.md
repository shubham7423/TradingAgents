# Codex Plugin Staged Workflow and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement US-009–US-013 as one restart-safe, revisioned Codex workflow from the selected analyst stages through a ready-to-finalize portfolio decision.

**Architecture:** Add one explicit `WorkflowService` that derives stages from frozen run inputs, rebuilds the existing role state from immutable accepted outputs, and calls the shared prompt/update helpers. Keep SQLite and cross-process transactions in `PluginStore`; keep MCP models, evidence paging, and operation registration in the existing plugin adapter. Persist canonical output and rendered Markdown together, with the stage-output row serving as the idempotency receipt.

**Tech Stack:** Python 3.10+, Pydantic v2, stdlib `sqlite3`/`datetime`/`hashlib`/`base64`/`json`/`uuid`, FastMCP 1.26.0, pytest, Ruff.

**Spec:** [`docs/superpowers/specs/2026-09-17-codex-plugin-staged-workflow-recovery-design.md`](../specs/2026-09-17-codex-plugin-staged-workflow-recovery-design.md)

## Global Constraints

- Deliver US-009–US-013 atomically; no partial feature-complete claim.
- Preserve Python 3.10–3.13 support and add no dependency.
- Never construct an LLM client, invoke a model, or launch another agent/process from the plugin workflow.
- Preserve existing API-runner prompts, output renderers, and accepted-output update behavior.
- Accept strings for narrative roles and JSON objects for `SentimentReport`, `ResearchPlan`, `TraderProposal`, and `PortfolioDecision`.
- Canonical JSON is authoritative; persist the rendered Markdown produced at acceptance time.
- Limit canonical serialized submissions to 32,000 characters.
- Use optimistic revisions and `BEGIN IMMEDIATE` for every submit/cancel mutation.
- Treat `success`, `no_data`, and `unavailable` as recorded evidence attempts; retryable errors remain unpersisted.
- Keep evidence reads bounded to 32,000 characters and analysis listings bounded to 100 rows per page.
- Reject unknown database, state, and prompt schema versions without deleting, resetting, or best-effort replay.
- Do not add a generic workflow engine, repository abstraction, migration framework, stage queue, background worker, or LangGraph checkpoint adapter.
- Do not implement US-014 report export, decision-history writes, or reflection behavior.

## File Map

- Modify `tradingagents/agents/analysts/sentiment_analyst.py`: expose the existing seven-day window calculation for both execution paths.
- Modify `tradingagents/plugin/store.py`: schema v2 migration, stage-output records, snapshot reads, filtered listings, atomic submission, and cancellation.
- Create `tradingagents/plugin/workflow.py`: explicit transition table, role-state replay, prompts, evidence requirements, validation/rendering, and workflow service.
- Modify `tradingagents/plugin/data.py`: public MCP response models, read modes, paging, and delegation to `WorkflowService`.
- Modify `tradingagents/plugin/server.py`: advertise/register the completed workflow surface.
- Modify `tests/test_shared_analyst_prompts.py`: shared sentiment-window regression.
- Modify `tests/test_plugin_store.py`: migration, immutable receipts, snapshot, listing, cancellation, and concurrency checks.
- Create `tests/test_plugin_workflow.py`: sequence, state replay, prompts, evidence, schema validation, submission, and cancellation tests.
- Modify `tests/test_plugin_data.py`: public read modes, paging, listings, and lifecycle adapter checks.
- Modify `tests/test_plugin_capabilities.py` and `tests/test_plugin_runtime.py`: tool discovery and protocol validation.
- Modify `docs/plugin-runtime.md`: document the completed staged workflow and recovery commands.
- Create `docs/user-testing-us-009-013.md`: record focused, full-suite, lint, and manual protocol checks.

---

### Task 1: Share the sentiment evidence window

**Files:**
- Modify: `tradingagents/agents/analysts/sentiment_analyst.py:50-105`
- Modify: `tests/test_shared_analyst_prompts.py:68-126`

**Interfaces:**
- Produces: `sentiment_window_start(trade_date: str) -> str`
- Preserves: `build_sentiment_prompt` and `create_sentiment_analyst(llm)` behavior

- [ ] **Step 1: Write the failing public-helper regression**

Add to `tests/test_shared_analyst_prompts.py`:

```python
def test_sentiment_window_start_is_shared_calendar_arithmetic():
    from tradingagents.agents.analysts.sentiment_analyst import sentiment_window_start

    assert sentiment_window_start("2026-03-01") == "2026-02-22"
```

- [ ] **Step 2: Run the focused test and verify the missing public helper**

Run:

```bash
pytest tests/test_shared_analyst_prompts.py::test_sentiment_window_start_is_shared_calendar_arithmetic -q
```

Expected: FAIL during import because `sentiment_window_start` does not exist.

- [ ] **Step 3: Rename and reuse the helper**

In `tradingagents/agents/analysts/sentiment_analyst.py`, replace the private helper and both internal calls:

```python
def sentiment_window_start(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
```

Use `sentiment_window_start(end_date)` in `build_sentiment_prompt()` and
`create_sentiment_analyst()`; leave the rest of each function unchanged.

- [ ] **Step 4: Run shared analyst regressions and lint**

Run:

```bash
pytest tests/test_shared_analyst_prompts.py -q
ruff check tradingagents/agents/analysts/sentiment_analyst.py tests/test_shared_analyst_prompts.py
```

Expected: all pass.

- [ ] **Step 5: Commit the shared date contract**

```bash
git add tradingagents/agents/analysts/sentiment_analyst.py tests/test_shared_analyst_prompts.py
git commit -m "refactor(analysts): share sentiment evidence window"
```

---

### Task 2: Migrate persistence and add immutable workflow reads

**Files:**
- Modify: `tradingagents/plugin/store.py:1-326`
- Modify: `tradingagents/plugin/data.py:1-1123`
- Modify: `tests/test_plugin_store.py`
- Modify: `tests/test_plugin_data.py:75-169`

**Interfaces:**
- Produces: `canonical_json(value: object) -> str`
- Produces: `digest_json(value: object) -> str`
- Extends: `RunRecord` with `ticker: str`
- Produces: `StageOutputRecord`, `AnalysisSnapshot`, and `EvidenceRequirement`
- Produces: `PluginStore.get_snapshot(run_id) -> AnalysisSnapshot`
- Produces: `PluginStore.get_stage_output(run_id, stage_id) -> StageOutputRecord | None`
- Produces: `PluginStore.get_evidence(run_id, evidence_id) -> EvidenceRecord`
- Produces: `PluginStore.list_runs(ticker, status, limit, before) -> list[RunRecord]`
- Preserves: existing run/evidence creation and reads across migration

- [ ] **Step 1: Add failing v1 migration and immutable-read tests**

Add a `_create_v1_database(path)` test helper to `tests/test_plugin_store.py` that creates the
current `metadata`, `runs`, and `evidence` tables, writes `schema_version=1`, and inserts one run
with `normalized_inputs_json` containing `{"ticker":"AAPL"}`. Then add:

```python
def test_v1_database_migrates_without_losing_run_or_evidence(tmp_path):
    database = tmp_path / "plugin.sqlite3"
    run_id, evidence_id = _create_v1_database(database)

    store = PluginStore(tmp_path)
    snapshot = store.get_snapshot(run_id)

    assert snapshot.run.ticker == "AAPL"
    assert snapshot.outputs == []
    assert [item.evidence_id for item in snapshot.evidence] == [evidence_id]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "2"


def test_snapshot_and_stage_output_reads_are_run_scoped(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())

    assert store.get_snapshot(run.run_id).run == run
    assert store.get_stage_output(run.run_id, "analyst/market") is None
    with pytest.raises(RunNotFound, match="RUN_NOT_FOUND"):
        store.get_evidence(run.run_id, "22222222-2222-4222-8222-222222222222")
```

Update `test_incompatible_schema_is_rejected_without_mutation` to expect database schema 2 while
continuing to prove value `999` is retained.

- [ ] **Step 2: Add failing listing-order tests**

Add to `tests/test_plugin_store.py`:

```python
def test_list_runs_filters_and_uses_stable_newest_first_boundary(tmp_path):
    store = PluginStore(tmp_path)
    first, _ = store.create_run(**run_values(now="2026-09-15T12:00:00+00:00"))
    second_values = run_values(now="2026-09-16T12:00:00+00:00") | {
        "request_id": "33333333-3333-4333-8333-333333333333",
        "normalized_inputs": {"ticker": "MSFT"},
        "instrument": {"requested_symbol": "MSFT", "canonical_symbol": "MSFT"},
    }
    second, _ = store.create_run(**second_values)

    assert [run.run_id for run in store.list_runs(None, None, 10, None)] == [
        second.run_id,
        first.run_id,
    ]
    assert [run.run_id for run in store.list_runs("AAPL", "active", 10, None)] == [
        first.run_id
    ]
    assert store.list_runs(None, None, 10, (second.created_at, second.run_id)) == [first]
```

Change `run_values` to accept `now` and keep its current default.

- [ ] **Step 3: Run the new store tests and verify schema/API failures**

Run:

```bash
pytest tests/test_plugin_store.py::test_v1_database_migrates_without_losing_run_or_evidence \
  tests/test_plugin_store.py::test_snapshot_and_stage_output_reads_are_run_scoped \
  tests/test_plugin_store.py::test_list_runs_filters_and_uses_stable_newest_first_boundary -q
```

Expected: FAIL because schema v1 is rejected and the new records/methods do not exist.

- [ ] **Step 4: Add public canonical serialization and schema-v2 records**

In `tradingagents/plugin/store.py`, set `SCHEMA_VERSION = 2`, replace `_json` with these shared
helpers, and update existing calls:

```python
import hashlib
from typing import Literal


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class StageOutputRecord:
    receipt_id: str
    run_id: str
    stage_id: str
    role_key: str
    output_kind: Literal["text", "structured"]
    canonical_output: object
    rendered_output: str
    output_hash: str
    accepted_revision: int
    accepted_at: str


@dataclass(frozen=True)
class AnalysisSnapshot:
    run: RunRecord
    outputs: list[StageOutputRecord]
    evidence: list[EvidenceRecord]


@dataclass(frozen=True)
class EvidenceRequirement:
    tool_name: str
    arguments: dict[str, object]
```

Add `ticker: str` to `RunRecord` and populate it from the new column. Import `canonical_json` and
`digest_json` in `tradingagents/plugin/data.py`; remove its duplicate `_canonical_json`/`_digest`
definitions and update cursor, request-hash, and argument-hash calls to the public names.

- [ ] **Step 5: Implement the one-step migration and fresh schema**

Refactor `_initialize()` into explicit fresh/v1 paths. The v1 path must run under
`BEGIN IMMEDIATE`, add `runs.ticker`, parse each row's `normalized_inputs_json` in Python, reject a
missing/non-string ticker with `INCOMPATIBLE_STATE`, create `stage_outputs`, add indexes, and only
then set the metadata value to `2`:

```python
CREATE TABLE stage_outputs (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    stage_id TEXT NOT NULL,
    role_key TEXT NOT NULL,
    output_kind TEXT NOT NULL CHECK (output_kind IN ('text','structured')),
    canonical_output_json TEXT NOT NULL,
    rendered_output TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    accepted_revision INTEGER NOT NULL,
    accepted_at TEXT NOT NULL,
    UNIQUE (run_id, stage_id)
)
```

Fresh v2 databases declare `runs.ticker TEXT NOT NULL`. Migrated databases may retain a nullable
column at the SQLite schema level, but every row must be backfilled. Add indexes:

```sql
CREATE INDEX idx_stage_outputs_run_revision
ON stage_outputs(run_id, accepted_revision);
CREATE INDEX idx_runs_listing
ON runs(status, ticker, created_at DESC, run_id DESC);
```

Set `ticker=normalized_inputs["ticker"]` on new run insertion without changing
`create_run`'s current public signature.

- [ ] **Step 6: Implement snapshot, scoped reads, and raw listing**

Use one connection per snapshot so run, outputs, and evidence share a read transaction. Order
outputs by `accepted_revision` and evidence by `(fetched_at, evidence_id)`. Implement:

Add `_stage_output_record(row)` beside the existing row converters, then implement the reads with
the existing `_run_record`/`_evidence_record` converters:

```python
def get_snapshot(self, run_id: str) -> AnalysisSnapshot:
    with self._connect() as connection:
        run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise RunNotFound(f"RUN_NOT_FOUND: analysis {run_id} does not exist")
        outputs = connection.execute(
            "SELECT * FROM stage_outputs WHERE run_id = ? ORDER BY accepted_revision",
            (run_id,),
        ).fetchall()
        evidence = connection.execute(
            "SELECT * FROM evidence WHERE run_id = ? ORDER BY fetched_at, evidence_id",
            (run_id,),
        ).fetchall()
    return AnalysisSnapshot(
        _run_record(run),
        [_stage_output_record(row) for row in outputs],
        [_evidence_record(row) for row in evidence],
    )

def get_stage_output(self, run_id: str, stage_id: str) -> StageOutputRecord | None:
    with self._connect() as connection:
        row = connection.execute(
            "SELECT * FROM stage_outputs WHERE run_id = ? AND stage_id = ?",
            (run_id, stage_id),
        ).fetchone()
    return None if row is None else _stage_output_record(row)

def get_evidence(self, run_id: str, evidence_id: str) -> EvidenceRecord:
    with self._connect() as connection:
        row = connection.execute(
            "SELECT * FROM evidence WHERE run_id = ? AND evidence_id = ?",
            (run_id, evidence_id),
        ).fetchone()
    if row is None:
        raise RunNotFound(f"RUN_NOT_FOUND: evidence {evidence_id} does not exist in {run_id}")
    return _evidence_record(row)
```

Build `list_runs` from a fixed allow-listed predicate list, never interpolate caller text into
SQL. Add `ticker = ?`, `status = ?`, and
`(created_at < ? OR (created_at = ? AND run_id < ?))` only when their corresponding arguments are
present; finish with `ORDER BY created_at DESC, run_id DESC LIMIT ?`. Pass `limit` as the last SQL
parameter and convert rows with `_run_record`.

- [ ] **Step 7: Run persistence and existing adapter regressions**

Run:

```bash
pytest tests/test_plugin_store.py tests/test_plugin_data.py -q
ruff check tradingagents/plugin/store.py tradingagents/plugin/data.py \
  tests/test_plugin_store.py tests/test_plugin_data.py
```

Expected: all pass, including existing run idempotency, evidence reuse, and paging tests.

- [ ] **Step 8: Commit schema-v2 read primitives**

```bash
git add tradingagents/plugin/store.py tradingagents/plugin/data.py \
  tests/test_plugin_store.py tests/test_plugin_data.py
git commit -m "feat(plugin): migrate workflow persistence"
```

---

### Task 3: Build the explicit stage engine and deterministic replay

**Files:**
- Create: `tradingagents/plugin/workflow.py`
- Create: `tests/test_plugin_workflow.py`

**Interfaces:**
- Produces: `role_key(stage_id: str) -> str`
- Produces: `next_stage(run: RunRecord, stage_id: str) -> tuple[str, str]`
- Produces: `initial_role_state(run: RunRecord) -> dict[str, object]`
- Produces: `rebuild_role_state(snapshot: AnalysisSnapshot) -> dict[str, object]`
- Consumes: `ANALYST_NODE_SPECS` and existing `apply_*_output` helpers

- [ ] **Step 1: Write failing exact-sequence tests**

Create `tests/test_plugin_workflow.py` with a `run_record` helper accepting keyword overrides and:

```python
def test_selected_analysts_and_rounds_define_exact_sequence():
    from tradingagents.plugin.workflow import next_stage

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
        "analyst/news", "analyst/market",
        "research/bull/1", "research/bear/1",
        "research/bull/2", "research/bear/2", "research/manager", "trader",
        "risk/aggressive/1", "risk/conservative/1", "risk/neutral/1",
        "risk/aggressive/2", "risk/conservative/2", "risk/neutral/2",
        "portfolio", "finalize",
    ]
    assert statuses[-1] == "ready_to_finalize"
    assert all(status == "active" for status in statuses[:-1])
```

Add parameterized rejection cases for malformed rounds, skipped analysts, `finalize`, and unknown
stage IDs; each must raise `IncompatibleState` rather than guessing.

- [ ] **Step 2: Write failing replay tests**

Build a snapshot containing rendered outputs for one analyst, bull, bear, research manager, trader,
and all three risk roles. Assert:

```python
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
```

Also assert the initial state contains empty reports/debates, the frozen instrument context,
analysis date, output language inputs, and frozen lessons.

- [ ] **Step 3: Run the focused tests and verify the module is absent**

Run:

```bash
pytest tests/test_plugin_workflow.py -q
```

Expected: collection failure because `tradingagents.plugin.workflow` does not exist.

- [ ] **Step 4: Implement role parsing and transitions**

Create `tradingagents/plugin/workflow.py` with explicit parsing, not a stored queue. Keep these
constants and signatures stable:

```python
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
    if len(parts) == 3 and parts[0] == "risk" and parts[1] in {
        "aggressive", "conservative", "neutral"
    }:
        return parts[1]
    raise IncompatibleState(f"INCOMPATIBLE_STATE: unknown stage {stage_id}")


def next_stage(run: RunRecord, stage_id: str) -> tuple[str, str]:
    inputs = run.normalized_inputs
    analysts = inputs["analysts"]
    if stage_id.startswith("analyst/"):
        key = role_key(stage_id)
        index = analysts.index(key)
        if index + 1 < len(analysts):
            return f"analyst/{analysts[index + 1]}", ACTIVE
        return "research/bull/1", ACTIVE
    if stage_id.startswith("research/bull/"):
        round_number = int(stage_id.rsplit("/", 1)[1])
        return f"research/bear/{round_number}", ACTIVE
    if stage_id.startswith("research/bear/"):
        round_number = int(stage_id.rsplit("/", 1)[1])
        if round_number < inputs["research_rounds"]:
            return f"research/bull/{round_number + 1}", ACTIVE
        return "research/manager", ACTIVE
    if stage_id == "research/manager":
        return "trader", ACTIVE
    if stage_id == "trader":
        return "risk/aggressive/1", ACTIVE
    if stage_id.startswith("risk/aggressive/"):
        return stage_id.replace("risk/aggressive/", "risk/conservative/"), ACTIVE
    if stage_id.startswith("risk/conservative/"):
        return stage_id.replace("risk/conservative/", "risk/neutral/"), ACTIVE
    if stage_id.startswith("risk/neutral/"):
        round_number = int(stage_id.rsplit("/", 1)[1])
        if round_number < inputs["risk_rounds"]:
            return f"risk/aggressive/{round_number + 1}", ACTIVE
        return "portfolio", ACTIVE
    if stage_id == "portfolio":
        return "finalize", READY_TO_FINALIZE
    raise IncompatibleState(f"INCOMPATIBLE_STATE: unknown stage {stage_id}")
```

Before returning from numbered branches, require round numbers from 1 through the corresponding
frozen round count. Convert missing analysts and malformed integer suffixes into
`IncompatibleState` instead of leaking `ValueError` or guessing.

- [ ] **Step 5: Implement initial state and replay through shared updates**

Initialize the exact existing fields:

```python
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
            "bull_history": "", "bear_history": "", "history": "",
            "current_response": "", "judge_decision": "", "count": 0,
        },
        "investment_plan": "",
        "trader_investment_plan": "",
        "risk_debate_state": {
            "aggressive_history": "", "conservative_history": "",
            "neutral_history": "", "history": "", "latest_speaker": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "", "judge_decision": "", "count": 0,
        },
        "final_trade_decision": "",
        "past_context": run.lessons,
    }
```

For analyst stages, assign rendered text through `ANALYST_NODE_SPECS[key].report_key`. For later
roles, merge the dict returned by the matching existing `apply_*_output(state, rendered_text)`.
Reject an output sequence that does not match repeated `next_stage()` calls.

- [ ] **Step 6: Run stage/replay and shared-role regressions**

Run:

```bash
pytest tests/test_plugin_workflow.py tests/test_shared_synthesis.py \
  tests/test_shared_analyst_prompts.py -q
ruff check tradingagents/plugin/workflow.py tests/test_plugin_workflow.py
```

Expected: all pass.

- [ ] **Step 7: Commit the pure workflow engine**

```bash
git add tradingagents/plugin/workflow.py tests/test_plugin_workflow.py
git commit -m "feat(plugin): define staged workflow sequence"
```

---

### Task 4: Add role prompts, schemas, evidence requirements, and output preparation

**Files:**
- Modify: `tradingagents/plugin/workflow.py`
- Modify: `tests/test_plugin_workflow.py`

**Interfaces:**
- Produces: `PreparedOutput`
- Produces: `EvidenceCheck`
- Produces: `StageView`
- Produces: `prepare_output(stage_id, output) -> PreparedOutput`
- Produces: `requirements_for(run, stage_id)` returning a tuple containing zero, one, or three `EvidenceRequirement` values
- Produces: `describe_stage(snapshot) -> StageView`
- Consumes: all shared role prompt builders, existing structured schemas, and renderers

- [ ] **Step 1: Write failing native-output validation tests**

Add to `tests/test_plugin_workflow.py`:

```python
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
```

Add cases for whitespace-only narrative output, wrong native kind, every structured schema, and a
canonical payload whose serialized length exceeds 32,000 characters.

- [ ] **Step 2: Write failing evidence and prompt-description tests**

Add tests asserting:

```python
def test_market_and_sentiment_require_exact_recorded_attempts():
    market = requirements_for(run_record(stage="analyst/market"), "analyst/market")
    social = requirements_for(run_record(stage="analyst/social"), "analyst/social")

    assert market == (EvidenceRequirement(
        "get_verified_market_snapshot",
        {"symbol": "AAPL", "curr_date": "2026-09-17"},
    ),)
    assert [item.tool_name for item in social] == [
        "get_news", "fetch_stocktwits_messages", "fetch_reddit_posts",
    ]
    assert all(item.arguments["start_date"] == "2026-09-10" for item in social)
    assert all(item.arguments["end_date"] == "2026-09-17" for item in social)
```

For `describe_stage(snapshot)`, assert active market/news/fundamentals prompts equal their shared
builder output; sentiment injects matching saved news/StockTwits/Reddit content; the opening bull
prompt contains the absent-opponent marker; trader instructions equal `build_trader_messages()`;
portfolio instructions include frozen lessons; structured stages expose exactly their model JSON
Schema. Missing sentiment evidence must appear as `<required evidence not yet recorded>` and in the
checklist, never as fabricated content.

- [ ] **Step 3: Run the focused tests and verify the contracts are missing**

Run:

```bash
pytest tests/test_plugin_workflow.py -q
```

Expected: FAIL for undefined preparation, evidence, and description interfaces.

- [ ] **Step 4: Implement exact DTOs and stable validation errors**

Add:

```python
MAX_SUBMISSION_SIZE = 32_000


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
```

Map `analyst/social`, `research/manager`, `trader`, and `portfolio` to their existing Pydantic model
and renderer. Validate with `model_validate()`, use `model_dump(mode="json")` for canonical output,
and translate `ValidationError.errors(include_url=False)` into `StageValidationError.errors`.
Narrative roles require a string with nonempty `.strip()` but preserve the submitted text exactly.
Measure `len(canonical_json(canonical_output))` before returning.

- [ ] **Step 5: Implement exact evidence requirements and satisfaction**

Use `sentiment_window_start()` and frozen canonical symbols. Match only the required argument
subset so caller-selected social limits and snapshot lookback length do not invalidate a correct
attempt. A record satisfies a requirement only when its `stage_id` is the pending stage,
`tool_name` matches, and every required argument has the same canonical value.

Return no requirements for other stages. Keep the ordering shown in the tests so prompts and error
responses are stable.

- [ ] **Step 6: Implement stage descriptions through shared builders**

Rebuild state once from the snapshot. Dispatch the current stage to:

```python
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
```

Call builders with `output_language=run.normalized_inputs["output_language"]`. Handle sentiment
separately because its builder takes the three recorded blocks. For inactive, cancelled, and
`ready_to_finalize` runs, return `StageView(None, None, [], None)`.

- [ ] **Step 7: Run workflow and shared-role regressions**

Run:

```bash
pytest tests/test_plugin_workflow.py tests/test_shared_synthesis.py \
  tests/test_shared_analyst_prompts.py tests/test_structured_agents.py \
  tests/test_structured_agent_prompts.py tests/test_signal_processing.py -q
ruff check tradingagents/plugin/workflow.py tests/test_plugin_workflow.py
```

Expected: all pass; no API-runner prompt behavior changes.

- [ ] **Step 8: Commit role contracts**

```bash
git add tradingagents/plugin/workflow.py tests/test_plugin_workflow.py
git commit -m "feat(plugin): prepare workflow role contracts"
```

---

### Task 5: Accept stages and cancel runs atomically

**Files:**
- Modify: `tradingagents/plugin/store.py`
- Modify: `tradingagents/plugin/workflow.py`
- Modify: `tests/test_plugin_store.py`
- Modify: `tests/test_plugin_workflow.py`

**Interfaces:**
- Produces: `PluginStore.accept_stage` returning `tuple[RunRecord, StageOutputRecord, bool]`
- Produces: `PluginStore.cancel_run(run_id, expected_revision, now) -> tuple[RunRecord, bool]`
- Produces: stable workflow exceptions/codes
- Produces: `WorkflowService.submit_stage` returning `AcceptedStage`
- Produces: `WorkflowService.cancel_analysis` returning `CancelledAnalysis`

- [ ] **Step 1: Write failing store idempotency and evidence-gate tests**

Add tests proving this exact ordering:

```python
def test_accept_stage_is_atomic_and_identical_retry_returns_receipt(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    values = prepared_stage_values(run)

    advanced, receipt, created = store.accept_stage(**values)
    retried, repeated, repeated_created = store.accept_stage(**values)

    assert created is True and repeated_created is False
    assert repeated == receipt
    assert retried == advanced
    assert advanced.current_stage == "research/bull/1"
    assert advanced.revision == 2
```

Add separate cases for a different hash on the accepted stage, stale revision, wrong stage,
cancelled run, missing evidence, unknown per-run schema, and two concurrent store instances using
the same revision. The two-writer assertion is one receipt, one revision increment, and one
identical-retry result or one stale failure depending on payload equality. Parameterize the
required-evidence success case over persisted `success`, `no_data`, and `unavailable` statuses.

- [ ] **Step 2: Write failing cancellation tests**

Add tests for active and ready-to-finalize cancellation, identical cancellation retry, stale
revision, completed-run rejection, and preservation of evidence/outputs. Assert first cancellation
increments revision once and preserves `current_stage`.

- [ ] **Step 3: Run focused store tests and verify mutation APIs are absent**

Run:

```bash
pytest tests/test_plugin_store.py -k 'accept_stage or cancel_run or two_writer' -q
```

Expected: FAIL for missing methods and error types.

- [ ] **Step 4: Implement stable store errors and atomic acceptance**

Add errors whose messages begin with their code:

```python
class MissingEvidence(RuntimeError):
    pass


class StaleRevision(RuntimeError):
    pass


class WrongStage(RuntimeError):
    pass


class StageAlreadyAccepted(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


class RunNotActive(RuntimeError):
    pass
```

Implement `accept_stage` with keyword-only `run_id`, `stage_id`, `expected_revision`, `role_key`,
`output_kind`, `canonical_output`, `rendered_output`, `output_hash`, `required_evidence`,
`next_stage`, `next_status`, and `now` parameters. Its return type is
`tuple[RunRecord, StageOutputRecord, bool]`. Do not add optional parameters or a second acceptance
path.

Inside one `BEGIN IMMEDIATE`: load the run; verify supported state/prompt versions; inspect the
submitted stage's existing row before revision/status checks; return an identical hash; reject a
different hash; check cancelled/active status, stage, and revision; query current-stage evidence;
match every requirement's argument subset; insert the output with
`accepted_revision=expected_revision`; update the run with `revision=revision+1`; and return both
rows. Roll back every exception.

- [ ] **Step 5: Implement atomic cancellation**

`cancel_run()` runs under `BEGIN IMMEDIATE`. Return `(run, False)` immediately for an already
cancelled run. Reject completed runs. Require `active` or `ready_to_finalize` plus the expected
revision, then set only `status='cancelled'`, `revision=revision+1`, and `updated_at`; preserve
`current_stage`.

- [ ] **Step 6: Write failing service-level submission tests**

Add to `tests/test_plugin_workflow.py`:

```python
def test_service_submits_selected_analyst_and_returns_next_stage(store):
    run = create_run(store, analysts=["market"])
    save_required_market_snapshot(store, run)
    service = WorkflowService(store)

    result = service.submit_stage(
        run.run_id, "analyst/market", 1, "Market report"
    )

    assert result.receipt.stage_id == "analyst/market"
    assert result.revision == 2
    assert result.current_stage == "research/bull/1"
```

Add structured field-error/no-advance, missing-evidence/no-advance, canonical retry, conflicting
retry, stale revision, out-of-order risk stage, final portfolio transition, and cancellation cases.

- [ ] **Step 7: Implement `WorkflowService` mutation methods**

The constructor is `WorkflowService(store: PluginStore)`. `submit_stage()` loads one snapshot,
checks compatibility, calls `prepare_output()`, obtains requirements, derives the next stage/status,
and calls `store.accept_stage()`. It does not hold a transaction while preparing output or building
prompts; the store repeats all mutable checks under its transaction.

Define exact service results:

```python
@dataclass(frozen=True)
class AcceptedStage:
    run_id: str
    status: Literal["active", "ready_to_finalize"]
    revision: int
    current_stage: str
    receipt: StageOutputRecord


@dataclass(frozen=True)
class CancelledAnalysis:
    run_id: str
    status: Literal["cancelled"]
    revision: int
    current_stage: str
    changed: bool
```

`cancel_analysis()` calls `store.cancel_run()` with a UTC ISO-8601 timestamp. Do not catch stable
workflow/store exceptions into generic messages; FastMCP must expose their codes.

- [ ] **Step 8: Run atomic workflow tests and lint**

Run:

```bash
pytest tests/test_plugin_store.py tests/test_plugin_workflow.py -q
ruff check tradingagents/plugin/store.py tradingagents/plugin/workflow.py \
  tests/test_plugin_store.py tests/test_plugin_workflow.py
```

Expected: all pass.

- [ ] **Step 9: Commit atomic workflow mutations**

```bash
git add tradingagents/plugin/store.py tradingagents/plugin/workflow.py \
  tests/test_plugin_store.py tests/test_plugin_workflow.py
git commit -m "feat(plugin): persist staged workflow submissions"
```

---

### Task 6: Expose analysis inspection, listing, submission, and cancellation

**Files:**
- Modify: `tradingagents/plugin/data.py:335-449,1014-1123`
- Modify: `tradingagents/plugin/server.py:20-155`
- Modify: `tests/test_plugin_data.py`
- Modify: `tests/test_plugin_capabilities.py`
- Modify: `tests/test_plugin_runtime.py`

**Interfaces:**
- Extends: `PluginTools.public_operations()` with `submit_stage`, `list_analyses`, and `cancel_analysis`
- Extends: `PluginTools.get_analysis(run_id, section, evidence_id, cursor, page_size)`
- Produces: Pydantic workflow DTOs with `extra="forbid"`
- Preserves: existing `start_analysis` and data-tool response fields

- [ ] **Step 1: Write failing public read-mode tests**

Add to `tests/test_plugin_data.py`:

```python
def test_get_analysis_default_section_and_evidence_modes(tools, store):
    run = create_completed_market_stage(store)
    default = tools.get_analysis(run.run_id)
    section = tools.get_analysis(run.run_id, section="analyst/market")
    evidence = tools.get_analysis(run.run_id, evidence_id=saved_evidence_id(store, run))

    assert default.active_role == "bull"
    assert default.instructions
    assert [item.stage_id for item in default.sections] == ["analyst/market"]
    assert section.selected_section.rendered_output == "Market report"
    assert section.instructions is None
    assert evidence.evidence_page.run_id == run.run_id
    assert evidence.instructions is None
```

Add mutual-exclusion, cursor-without-evidence, cross-run evidence ID, saved-section-not-found,
32,000-character continuation, cancelled-run no-prompt, and ready-to-finalize no-prompt cases.

- [ ] **Step 2: Write failing listing and lifecycle adapter tests**

Add tests that call `tools.list_analyses()` with ticker/status filters, a page size of one, and the
returned cursor; assert no duplicate. Reject limits outside 1–100 and malformed cursors. Add native
`tools.submit_stage` and revisioned `tools.cancel_analysis` tests that assert serialized
receipts and stable error codes.

- [ ] **Step 3: Write failing discovery/schema tests**

Add `submit_stage`, `list_analyses`, and `cancel_analysis` to `EXPECTED_TOOLS` in both capability
and runtime tests. Assert `submit_stage.inputSchema["properties"]["output"]` allows string or object,
every operation forbids additional properties, and discovery does not import an LLM client.

- [ ] **Step 4: Run public-surface tests and verify missing operations**

Run:

```bash
pytest tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q
```

Expected: FAIL because the new operations/read modes are not registered.

- [ ] **Step 5: Extend response models without breaking start responses**

Keep current `AnalysisResult` fields and add optional/defaulted fields:

```python
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
```

Add these exact listing and outer models:

```python
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
```

- [ ] **Step 6: Implement three read modes and run listing cursors**

Construct `self._workflow = WorkflowService(store)` in `PluginTools.__init__`. Every
`get_analysis` mode first loads one snapshot to validate the run and provide common fields. Default
mode calls `describe_stage()`. Section mode calls scoped `get_stage_output`; evidence mode calls
scoped `get_evidence` then `_page_record(..., reused=True)`.

Encode listing cursors as URL-safe base64 canonical JSON containing exactly `created_at` and
`run_id`; validate UUID/string shape and canonical round-trip as strictly as evidence cursors.
Fetch `limit + 1`, return at most `limit`, and create the next cursor from the last returned row
only when another row exists. Normalize a non-null ticker with `_validate_ticker` before passing it
to the store.

- [ ] **Step 7: Delegate mutation operations and register the surface**

Add thin methods with these public signatures. Delegate to `WorkflowService`, then convert its
dataclasses through private `_submission_result`, `_list_result`, and `_cancellation_result`
adapters that populate every response field and do not catch workflow errors:

```python
def submit_stage(
    self,
    run_id: str,
    stage_id: str,
    expected_revision: int,
    output: str | dict,
) -> SubmissionResult:
    accepted = self._workflow.submit_stage(run_id, stage_id, expected_revision, output)
    return self._submission_result(accepted)

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
    cancelled = self._workflow.cancel_analysis(run_id, expected_revision)
    return self._cancellation_result(cancelled)
```

Add them to `public_operations()`, `get_capabilities().available_tools`, and set every non-reflection
role's `workflow_available=True`. Keep reflection false and `get_decision_history` planned.

- [ ] **Step 8: Run adapter/protocol tests and lint**

Run:

```bash
pytest tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q
ruff check tradingagents/plugin/data.py tradingagents/plugin/server.py \
  tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py
```

Expected: all pass; stdout protocol tests remain JSON-RPC only.

- [ ] **Step 9: Commit the public workflow surface**

```bash
git add tradingagents/plugin/data.py tradingagents/plugin/server.py \
  tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py
git commit -m "feat(plugin): expose staged workflow recovery"
```

---

### Task 7: Prove the atomic milestone and document operation

**Files:**
- Modify: `tests/test_plugin_workflow.py`
- Modify: `tests/test_plugin_runtime.py`
- Modify: `docs/plugin-runtime.md`
- Create: `docs/user-testing-us-009-013.md`

**Interfaces:**
- Verifies: one installed-style MCP path with no model-client construction
- Verifies: N=2/R=2 produces four research and six risk contributions
- Documents: exact start/read/fetch/submit/list/cancel lifecycle

- [ ] **Step 1: Add the complete fixture-backed workflow test**

Add one parameterized helper that submits valid native outputs for every role. The N=2/R=2 test
must start with selected analysts `market` and `social`, save the exact required evidence records,
reopen `PluginStore` at one analyst, one research, and one risk boundary, and finish with:

```python
final = reopened.get_snapshot(run_id)
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
```

Assert each reopened `describe_stage()` uses saved evidence/outputs and frozen configuration, and
patch the LLM-client factory to raise if imported or called.

- [ ] **Step 2: Add an SDK-level workflow call sequence**

Extend `tests/test_plugin_runtime.py` with an in-memory FastMCP client sequence that starts a
single-news-analyst run, reads its prompt, submits the analyst, rejects a stale bull submission,
lists the run, cancels it at the current revision, and confirms later submission fails with
`RUN_CANCELLED`. Stub identity/memory only; do not invoke a vendor or model.

- [ ] **Step 3: Run the integrated tests and verify any remaining failure**

Run:

```bash
pytest tests/test_plugin_workflow.py::test_complete_multi_round_run_survives_restarts \
  tests/test_plugin_runtime.py::test_sdk_executes_and_cancels_workflow -q
```

Expected: PASS. If a focused integration failure exposes a mismatched DTO, cursor, prompt, or
transaction boundary, fix only that demonstrated mismatch and rerun until both pass.

- [ ] **Step 4: Document the exact runtime lifecycle**

Update `docs/plugin-runtime.md` with:

- Available `submit_stage`, `list_analyses`, and `cancel_analysis` operations.
- Native text/object output rule and 32,000-character limit.
- Evidence-before-submit flow for market and sentiment.
- Revision, identical-retry, cancellation, and `ready_to_finalize` semantics.
- A note that `finalize` is not submittable and export arrives in US-014.

Create `docs/user-testing-us-009-013.md` with checkboxes and command/output slots for the focused
tests below, `pytest -q`, `ruff check .`, and one local stdio discovery/call run. State explicitly
that this milestone does not export reports or memory.

- [ ] **Step 5: Run all focused workflow regressions**

Run:

```bash
pytest tests/test_plugin_store.py tests/test_plugin_workflow.py tests/test_plugin_data.py \
  tests/test_plugin_capabilities.py tests/test_plugin_runtime.py \
  tests/test_shared_analyst_prompts.py tests/test_shared_synthesis.py \
  tests/test_structured_agents.py tests/test_structured_agent_prompts.py \
  tests/test_signal_processing.py -q
```

Expected: all pass.

- [ ] **Step 6: Run the repository release gates**

Run:

```bash
pytest -q
ruff check .
python -m pip wheel . --no-deps -w /tmp/tradingagents-dist
```

Expected: full suite and Ruff pass; wheel builds successfully without installing plugin-only
dependencies into the base package.

- [ ] **Step 7: Record verification evidence**

Fill `docs/user-testing-us-009-013.md` with the actual date, commands, pass counts, lint result,
wheel filename, and protocol observations. Do not write expected results as if they were observed.

- [ ] **Step 8: Commit the milestone verification**

```bash
git add tests/test_plugin_workflow.py tests/test_plugin_runtime.py \
  docs/plugin-runtime.md docs/user-testing-us-009-013.md
git commit -m "test(plugin): verify staged workflow recovery"
```
