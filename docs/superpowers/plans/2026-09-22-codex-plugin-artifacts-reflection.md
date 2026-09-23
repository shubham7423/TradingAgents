# Codex Plugin Artifacts and Reflection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete US-014–016 by adding retry-safe report and decision export, point-in-time history, five-session outcome preparation, and durable Codex-authored reflection jobs.

**Architecture:** Keep Markdown as canonical decision history and SQLite as the plugin workflow ledger. Reuse the current report writer, state replay, prompt builder, rating schema, benchmark rules, and memory context filter; add only one reflection-job table, one export-receipt table, and small dispatch branches in the existing workflow facade.

**Tech Stack:** Python 3.10–3.13, stdlib SQLite/file locking/atomic replace, Pydantic, yfinance, pytest, Ruff, MCP FastMCP.

**Spec:** `docs/superpowers/specs/2026-09-21-codex-plugin-artifacts-reflection-design.md`

## Global Constraints

- Markdown at the configured `memory_log_path` remains the canonical shared history.
- Outcomes always use exactly five trading sessions; never parse `PortfolioDecision.time_horizon`.
- Plugin paths must not construct an LLM client or invoke `Reflector`.
- All memory mutations must serialize across processes and atomically replace the file.
- Public tools never accept a report directory, memory path, function name, or executable content.
- Preserve Python 3.10 compatibility and add no runtime dependency.
- Preserve existing API-runner behavior and legacy memory readability.
- Automatic reflection-before-analysis orchestration is deferred to US-017; this plan records explicit omission only.

## Review Focus

- Two processes append/update the same memory log: both entries survive and the file remains parseable (Task 2).
- A crash after memory export but before SQLite completion: retry returns one decision and one receipt (Task 4).
- A historical cutoff falls between decision and resolution dates: history shows the decision as pending and preparation creates no future-aware job (Tasks 2 and 5).
- Two indistinguishable legacy pending blocks exist: history warns and preparation refuses to target either (Tasks 2 and 5).
- A decision is resolved by the API runner after job preparation: plugin finalization preserves it and completes with `changed=false` (Task 5).

---

## File Map

- Modify `tradingagents/graph/reflection.py`: model-free benchmark and five-session outcome helpers beside the existing reflection prompt.
- Modify `tradingagents/graph/trading_graph.py`: delegate existing benchmark/return methods to shared helpers.
- Modify `tradingagents/reporting.py`: accept an optional stable generation timestamp.
- Modify `tradingagents/agents/utils/memory.py`: stable identities, metadata parsing, point-in-time projection, cross-process locking, and atomic writes.
- Modify `tradingagents/plugin/store.py`: schema v3, export receipts, reflection jobs, and atomic transitions.
- Modify `tradingagents/plugin/workflow.py`: analysis finalization, reflection preparation/stages/finalization, and work-ID dispatch.
- Modify `tradingagents/plugin/data.py`: public request/response models, history paging, omission flag, and new operations.
- Modify `tradingagents/plugin/server.py`: schema/tool/role capability updates.
- Modify `tests/test_reporting.py`, `tests/test_memory_log.py`, `tests/test_memory_pointintime.py`, `tests/test_plugin_store.py`, `tests/test_plugin_workflow.py`, `tests/test_plugin_data.py`, `tests/test_plugin_capabilities.py`, and `tests/test_plugin_runtime.py`.
- Create `docs/user-testing-us-014-016.md`: automated and manual verification record.

### Task 1: Share deterministic outcomes and stable report timestamps

**Files:**
- Modify: `tradingagents/graph/reflection.py`
- Modify: `tradingagents/graph/trading_graph.py:250-365`
- Modify: `tradingagents/reporting.py`
- Test: `tests/test_memory_log.py`
- Test: `tests/test_reporting.py`

**Interfaces:**
- Produces: `Outcome`, `resolve_benchmark(config, ticker)`, and `calculate_outcome(ticker, trade_date, config, holding_sessions=5, as_of_date=None)`.
- Produces: `write_report_tree(final_state, ticker, save_path, generated_at=None) -> Path`.
- Preserves: `TradingAgentsGraph._resolve_benchmark()` and `_fetch_returns()` signatures for existing callers.

- [ ] **Step 1: Write failing outcome-helper tests**

Add tests proving the shared helper returns the same five-session values, honors suffix/explicit benchmarks, and rejects a resolution after `as_of_date`:

```python
from tradingagents.graph.reflection import calculate_outcome, resolve_benchmark


def test_calculate_outcome_uses_fifth_session_and_cutoff(monkeypatch):
    frames = {
        "AAPL": _price_df([100, 101, 102, 103, 104, 110], "2026-01-05"),
        "SPY": _price_df([400, 401, 402, 403, 404, 420], "2026-01-05"),
    }
    monkeypatch.setattr(
        "tradingagents.graph.reflection.yf.Ticker",
        lambda symbol: SimpleNamespace(history=lambda **_: frames[symbol]),
    )
    config = {"benchmark_ticker": None, "benchmark_map": {"": "SPY"}}

    outcome = calculate_outcome("AAPL", "2026-01-05", config)

    assert outcome.holding_sessions == 5
    assert outcome.resolution_date == "2026-01-10"
    assert outcome.benchmark == "SPY"
    assert outcome.raw_return == pytest.approx(0.10)
    assert outcome.alpha_return == pytest.approx(0.05)
    assert calculate_outcome(
        "AAPL", "2026-01-05", config, as_of_date="2026-01-09"
    ) is None


def test_resolve_benchmark_prefers_explicit_then_suffix():
    assert resolve_benchmark(
        {"benchmark_ticker": "QQQ", "benchmark_map": {".T": "^N225", "": "SPY"}},
        "7203.T",
    ) == "QQQ"
    assert resolve_benchmark(
        {"benchmark_ticker": None, "benchmark_map": {".T": "^N225", "": "SPY"}},
        "7203.T",
    ) == "^N225"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_memory_log.py -k 'calculate_outcome or resolve_benchmark' -q`

Expected: FAIL because the module-level interfaces do not exist.

- [ ] **Step 3: Implement the shared helpers and delegate graph methods**

Add to `reflection.py`:

```python
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
        raw = float(
            (stock["Close"].iloc[holding_sessions] - stock["Close"].iloc[0])
            / stock["Close"].iloc[0]
        )
        benchmark_return = float(
            (bench["Close"].iloc[holding_sessions] - bench["Close"].iloc[0])
            / bench["Close"].iloc[0]
        )
        return Outcome(raw, raw - benchmark_return, holding_sessions, benchmark, resolution_date)
    except Exception as exc:
        logger.warning("Could not calculate outcome for %s on %s: %s", ticker, trade_date, exc)
        return None
```

Import `dataclass`, `datetime`, `timedelta`, `logging`, `Mapping`, `Any`, `yfinance as yf`, and
the existing `normalize_symbol`; define `logger = logging.getLogger(__name__)`. Add
`from types import SimpleNamespace` to the test module.

Make graph methods one-line compatibility delegates returning the existing tuple shape.

- [ ] **Step 4: Add and pass the stable timestamp regression**

Add:

```python
def test_write_report_tree_accepts_stable_generated_at(tmp_path):
    generated = datetime(2026, 9, 21, 12, 30, 0)
    out = write_report_tree(_state(), "AAPL", tmp_path, generated_at=generated)
    assert "Generated: 2026-09-21 12:30:00" in out.read_text()
```

Change `write_report_tree` to accept `generated_at: datetime | None = None` and render
`generated_at or datetime.now()`. Run:

`pytest tests/test_memory_log.py tests/test_reporting.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/graph/reflection.py tradingagents/graph/trading_graph.py tradingagents/reporting.py tests/test_memory_log.py tests/test_reporting.py
git commit -m "refactor(reflection): share deterministic outcome calculation"
```

### Task 2: Make canonical memory identity-safe and process-safe

**Files:**
- Modify: `tradingagents/agents/utils/memory.py`
- Test: `tests/test_memory_log.py`
- Test: `tests/test_memory_pointintime.py`

**Interfaces:**
- Produces: `store_decision(ticker, trade_date, final_trade_decision, decision_id=None) -> bool`.
- Produces: `resolve_decision(*, decision_id, ticker, trade_date, raw_return, alpha_return, holding_days, benchmark_name, resolution_date, reflection) -> Literal["updated", "identical", "already_resolved", "missing", "ambiguous"]`.
- Produces: `get_history(ticker=None, as_of=None) -> tuple[list[dict], list[str]]`.
- Preserves: `load_entries`, `get_pending_entries`, `get_past_context`, `update_with_outcome`, and `batch_update_with_outcomes` behavior for old callers.

- [ ] **Step 1: Write failing identity, point-in-time, and ambiguity tests**

```python
def test_decision_identity_is_idempotent_and_preserved_on_resolution(tmp_path):
    log = make_log(tmp_path)
    assert log.store_decision("AAPL", "2026-01-05", DECISION_BUY, decision_id="run-1")
    assert not log.store_decision("AAPL", "2026-01-05", DECISION_BUY, decision_id="run-1")
    assert log.resolve_decision(
        decision_id="run-1", ticker="AAPL", trade_date="2026-01-05",
        raw_return=0.10, alpha_return=0.05, holding_days=5,
        benchmark_name="SPY", resolution_date="2026-01-10", reflection="Lesson.",
    ) == "updated"
    entry = log.load_entries()[0]
    assert entry["decision_id"] == "run-1"
    assert entry["benchmark"] == "SPY"


def test_history_projects_future_resolution_as_pending(tmp_path):
    log = make_log(tmp_path)
    log.store_decision("AAPL", "2026-01-05", DECISION_BUY, decision_id="run-1")
    log.resolve_decision(
        decision_id="run-1", ticker="AAPL", trade_date="2026-01-05",
        raw_return=0.10, alpha_return=0.05, holding_days=5,
        benchmark_name="SPY", resolution_date="2026-01-10", reflection="Lesson.",
    )
    entries, warnings = log.get_history(ticker="AAPL", as_of="2026-01-09")
    assert warnings == []
    assert entries[0]["pending"] is True
    assert entries[0]["raw"] is None
    assert entries[0]["reflection"] == ""


def test_duplicate_legacy_identity_is_ambiguous(tmp_path):
    path = tmp_path / "trading_memory.md"
    block = f"[2026-01-05 | AAPL | Buy | pending]\n\nDECISION:\n{DECISION_BUY}"
    path.write_text(block + TradingMemoryLog._SEPARATOR + block + TradingMemoryLog._SEPARATOR)
    log = TradingMemoryLog({"memory_log_path": str(path)})
    entries, warnings = log.get_history(ticker="AAPL")
    assert len(entries) == 2
    assert any("ambiguous legacy identity" in warning for warning in warnings)


def test_history_skips_malformed_block_with_warning(tmp_path):
    path = tmp_path / "trading_memory.md"
    path.write_text("not an entry" + TradingMemoryLog._SEPARATOR, encoding="utf-8")
    entries, warnings = TradingMemoryLog({"memory_log_path": str(path)}).get_history()
    assert entries == []
    assert any("malformed memory entry" in warning for warning in warnings)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_memory_log.py tests/test_memory_pointintime.py -k 'identity or future_resolution or ambiguous' -q`

Expected: FAIL because identity/history APIs do not exist.

- [ ] **Step 3: Implement marker parsing and point-in-time projection**

Use `<!-- decision_id: VALUE -->` before `DECISION:`. Derive unmarked IDs with SHA-256 over
`json.dumps([date, ticker, rating, decision], separators=(",", ":"))`. Parse optional trailing `benchmark:VALUE` and
existing `resolved:YYYY-MM-DD` fields. `get_history` filters decisions after `as_of`; when a
resolution is later than `as_of`, return a copied pending projection with outcome/reflection fields
cleared. Detect duplicate derived IDs and add warnings; never silently select one.

Implement these exact public signatures: `store_decision(self, ticker, trade_date,
final_trade_decision, decision_id=None) -> bool`; `resolve_decision(self, *, decision_id, ticker,
trade_date, raw_return, alpha_return, holding_days, benchmark_name, resolution_date, reflection)
-> str`; and `get_history(self, ticker=None, as_of=None) -> tuple[list[dict], list[str]]`.

- [ ] **Step 4: Write the failing two-process lost-update test**

Add a module-level multiprocessing worker and test:

```python
def _store_in_process(path):
    return TradingMemoryLog({"memory_log_path": str(path)}).store_decision(
        "MSFT", "2026-01-05", DECISION_BUY, decision_id="run-b"
    )


def _resolve_in_process(path):
    return TradingMemoryLog({"memory_log_path": str(path)}).resolve_decision(
        decision_id="run-a", ticker="AAPL", trade_date="2026-01-05",
        raw_return=0.10, alpha_return=0.05, holding_days=5,
        benchmark_name="SPY", resolution_date="2026-01-10", reflection="Lesson.",
    )


def test_concurrent_append_and_update_preserve_both_entries(tmp_path):
    path = tmp_path / "trading_memory.md"
    TradingMemoryLog({"memory_log_path": str(path)}).store_decision(
        "AAPL", "2026-01-05", DECISION_BUY, decision_id="run-a"
    )
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_store_in_process, path), pool.submit(_resolve_in_process, path)]
        assert [future.result() for future in futures] == [True, "updated"]
    entries = TradingMemoryLog({"memory_log_path": str(path)}).load_entries()
    assert {entry["decision_id"] for entry in entries} == {"run-a", "run-b"}
    assert next(entry for entry in entries if entry["decision_id"] == "run-a")["pending"] is False
```

Add `from concurrent.futures import ProcessPoolExecutor` to the test module.

Run: `pytest tests/test_memory_log.py -k concurrent_append -q`

Expected: FAIL or expose the current unlocked append race.

- [ ] **Step 5: Implement one lock-and-replace mutation path**

Add a private context manager using a sibling `.lock` file, `fcntl.flock` on POSIX and
`msvcrt.locking` on Windows. Route append, single update, batch update, and rotation through one
`_mutate(transform)` helper that locks, rereads, transforms, writes a same-directory temporary
file, and calls `Path.replace`. Add `# ponytail: one lock serializes the small local log; shard only
if measured write contention matters` beside the lock.

- [ ] **Step 6: Run memory regressions**

Run: `pytest tests/test_memory_log.py tests/test_memory_pointintime.py -q`

Expected: PASS, including legacy six-field tags and current API-runner tests.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/agents/utils/memory.py tests/test_memory_log.py tests/test_memory_pointintime.py
git commit -m "fix(memory): serialize identity-based history updates"
```

### Task 3: Persist export receipts and reflection jobs in schema v3

**Files:**
- Modify: `tradingagents/plugin/store.py`
- Test: `tests/test_plugin_store.py`

**Interfaces:**
- Produces dataclasses: `ExportReceipt` and `ReflectionJobRecord`.
- Produces: `get_export_receipt(run_id) -> ExportReceipt | None` and
  `complete_analysis_export(*, run_id, decision_id, report_dir, complete_report_path,
  section_paths, rating, now) -> tuple[ExportReceipt, bool]`.
- Produces: `create_reflection_job(*, decision_id, ticker, decision_date, rating, decision_text,
  raw_return, alpha_return, holding_sessions, benchmark, resolution_date, now)
  -> tuple[ReflectionJobRecord, bool]` and `get_reflection_job(job_id) -> ReflectionJobRecord`.
- Produces: `accept_reflection(*, job_id, expected_revision, reflection, reflection_hash, now)
  -> tuple[ReflectionJobRecord, bool]` and `complete_reflection(*, job_id, changed, warning, now)
  -> tuple[ReflectionJobRecord, bool]`.
- Consumes: existing `canonical_json`, `digest_json`, transaction/error conventions.

- [ ] **Step 1: Write failing v2 migration and persistence tests**

Create a v2 fixture from the current schema, open it with `PluginStore`, and assert v3 tables and
existing rows survive. Add:

```python
def test_v2_database_migrates_to_export_and_reflection_tables(tmp_path):
    database, run_id = _create_v2_database(tmp_path / "plugin.sqlite3")
    store = PluginStore(tmp_path)
    assert store.get_run(run_id).run_id == run_id
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "3"
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"export_receipts", "reflection_jobs"} <= names


def test_reflection_job_is_unique_by_decision_and_survives_restart(tmp_path):
    store = PluginStore(tmp_path)
    values = reflection_job_values(decision_id="legacy-1")
    first, created = store.create_reflection_job(**values)
    repeated, repeated_created = store.create_reflection_job(**values)
    assert created is True and repeated_created is False
    assert repeated == first == PluginStore(tmp_path).get_reflection_job(first.job_id)
```

Before changing production schema code, add `_create_v2_database(path)` beside the existing v1
fixture. It executes the current v2 `metadata`, `runs`, `evidence`, and `stage_outputs` definitions,
sets `schema_version` to `2`, inserts one run, and returns `(path, run_id)`. Copy those definitions
verbatim from the pre-change `_create_schema`; do not call production migration code from the
fixture.

- [ ] **Step 2: Run store tests and verify failure**

Run: `pytest tests/test_plugin_store.py -k 'v2_database or reflection_job' -q`

Expected: FAIL with schema version 2 expected or missing methods.

- [ ] **Step 3: Add exact v3 schema and migration**

Set `SCHEMA_VERSION = 3`. Make v1 migrate to v2 first, then v2 to v3. Add:

```sql
CREATE TABLE export_receipts (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    decision_id TEXT NOT NULL UNIQUE,
    report_dir TEXT NOT NULL,
    complete_report_path TEXT NOT NULL,
    section_paths_json TEXT NOT NULL,
    rating TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE TABLE reflection_jobs (
    job_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    rating TEXT NOT NULL,
    decision_text TEXT NOT NULL,
    raw_return REAL NOT NULL,
    alpha_return REAL NOT NULL,
    holding_sessions INTEGER NOT NULL CHECK (holding_sessions = 5),
    benchmark TEXT NOT NULL,
    resolution_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','ready_to_finalize','completed')),
    revision INTEGER NOT NULL,
    current_stage TEXT NOT NULL,
    reflection TEXT,
    reflection_hash TEXT,
    changed INTEGER,
    warning TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
```

- [ ] **Step 4: Add atomic transition tests**

Test identical/conflicting reflection submission, stale revision, ready-to-finalize completion,
completed retry, and analysis receipt completion. In particular:

```python
def reflection_job_values(decision_id="decision-1"):
    return {
        "decision_id": decision_id, "ticker": "AAPL", "decision_date": "2026-01-05",
        "rating": "Buy", "decision_text": "Buy with controlled sizing.",
        "raw_return": 0.10, "alpha_return": 0.05, "holding_sessions": 5,
        "benchmark": "SPY", "resolution_date": "2026-01-10",
        "now": "2026-01-10T12:00:00+00:00",
    }


def ready_run(store):
    run, _ = store.create_run(**run_values())
    _set_run_fields(store, run.run_id, status="ready_to_finalize", current_stage="finalize")
    return store.get_run(run.run_id)


def export_values(run_id):
    return {
        "run_id": run_id, "decision_id": run_id,
        "report_dir": f"/results/plugin/{run_id}",
        "complete_report_path": f"/results/plugin/{run_id}/complete_report.md",
        "section_paths": [f"/results/plugin/{run_id}/5_portfolio/decision.md"],
        "rating": "Hold", "now": "2026-01-10T12:00:00+00:00",
    }


def test_complete_analysis_export_is_atomic_and_idempotent(tmp_path):
    store = PluginStore(tmp_path)
    run = ready_run(store)
    values = export_values(run.run_id)
    first, created = store.complete_analysis_export(**values)
    repeated, repeated_created = store.complete_analysis_export(**values)
    assert created is True and repeated_created is False
    assert repeated == first
    assert store.get_run(run.run_id).status == "completed"
```

- [ ] **Step 5: Implement minimal dataclasses and store methods**

Use existing `BEGIN IMMEDIATE`, revision checks, canonical JSON, and error classes. Reflection
submission hashes the string and returns the accepted row on an identical retry; a different hash
raises `StageAlreadyAccepted`. Completion records `changed` and optional warning and increments the
revision once. Analysis completion inserts its receipt and updates the run to `completed` in the
same transaction.

- [ ] **Step 6: Run all store tests**

Run: `pytest tests/test_plugin_store.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/plugin/store.py tests/test_plugin_store.py
git commit -m "feat(plugin): persist exports and reflection jobs"
```

### Task 4: Finalize analyses with retry-safe reports and decisions

**Files:**
- Modify: `tradingagents/plugin/workflow.py`
- Modify: `tradingagents/plugin/data.py`
- Test: `tests/test_plugin_workflow.py`
- Test: `tests/test_plugin_data.py`

**Interfaces:**
- Consumes: `rebuild_role_state`, `write_report_tree`, identity-aware `TradingMemoryLog`, and export receipt store methods.
- Produces: `WorkflowService.finalize_analysis(work_id) -> AnalysisFinalization | ReflectionFinalization` (analysis branch in this task).
- Produces public `PluginTools.finalize_analysis(run_id)` and `AnalysisFinalizationResult`.

- [ ] **Step 1: Write failing successful-finalization test**

Build a real ready-to-finalize snapshot by accepting the portfolio stage, configure temp results
and memory paths, then assert:

```python
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


def create_ready_analysis(store, server_config):
    run = create_run(store, analysts=["news"])
    service = WorkflowService(store, server_config)
    while run.current_stage != "finalize":
        service.submit_stage(
            run.run_id, run.current_stage, run.revision,
            valid_stage_output(run.current_stage),
        )
        run = store.get_run(run.run_id)
    return run


result = WorkflowService(store, server_config).finalize_analysis(run.run_id)
assert result.run_id == run.run_id
assert result.rating == "Hold"
assert result.decision_id == run.run_id
assert Path(result.complete_report_path).exists()
assert store.get_run(run.run_id).status == "completed"
assert memory.load_entries()[0]["decision_id"] == run.run_id
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/test_plugin_workflow.py -k finalize_analysis -q`

Expected: FAIL because finalization is absent.

- [ ] **Step 3: Implement the analysis branch**

Change `WorkflowService.__init__` to accept a copied server config. For a ready analysis:

1. replay state with `rebuild_role_state`;
2. read the portfolio output's canonical `rating`;
3. write to `Path(results_dir) / "plugin" / run_id` using the parsed `run.created_at` timestamp;
4. collect emitted `.md` paths with `sorted(report_dir.rglob("*.md"))`;
5. call `store_decision(run.ticker, analysis_date, state["final_trade_decision"], decision_id=run_id)`; and
6. call `complete_analysis_export`.

Check for an existing receipt before filesystem work. Reject statuses other than
`ready_to_finalize` or completed-with-receipt.

- [ ] **Step 4: Write the post-memory fault test**

Monkeypatch `store.complete_analysis_export` to fail once after memory write, restore it, and retry:

```python
with pytest.raises(RuntimeError, match="injected"):
    service.finalize_analysis(run.run_id)
assert len(memory.load_entries()) == 1
result = service.finalize_analysis(run.run_id)
assert len(memory.load_entries()) == 1
assert store.get_run(run.run_id).status == "completed"
assert result.decision_id == run.run_id
```

Also patch `write_report_tree` to raise before memory export and assert the run remains
`ready_to_finalize`, no export receipt exists, and the memory log stays empty.

- [ ] **Step 5: Add the typed public method**

Add `finalize_analysis` to `PluginTools.public_operations()`. Map the internal receipt to a strict
Pydantic response containing `run_id`, `work_type="analysis"`, `status="completed"`, `rating`,
`decision_id`, `report_dir`, `complete_report_path`, `section_paths`, and `changed`.

```python
class AnalysisFinalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    work_type: Literal["analysis"] = "analysis"
    status: Literal["completed"]
    rating: str
    decision_id: str
    report_dir: str
    complete_report_path: str
    section_paths: list[str]
    changed: bool
```

- [ ] **Step 6: Run workflow and data tests**

Run: `pytest tests/test_plugin_workflow.py tests/test_plugin_data.py -k 'finaliz or export' -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/plugin/workflow.py tradingagents/plugin/data.py tests/test_plugin_workflow.py tests/test_plugin_data.py
git commit -m "feat(plugin): finalize reports and decisions safely"
```

### Task 5: Prepare and complete durable reflection jobs

**Files:**
- Modify: `tradingagents/plugin/workflow.py`
- Modify: `tradingagents/plugin/data.py`
- Test: `tests/test_plugin_workflow.py`
- Test: `tests/test_plugin_data.py`

**Interfaces:**
- Consumes: `calculate_outcome`, `build_reflection_messages`, memory `get_history/resolve_decision`, and reflection-job store methods.
- Produces: `WorkflowService.prepare_reflections(ticker, as_of_date)`, reflection-aware `describe_work`, `submit_stage`, and `finalize_analysis`.
- Produces public `prepare_reflections`, plus reflection variants returned by existing lifecycle tools.

- [ ] **Step 1: Write failing preparation tests**

Patch `calculate_outcome` with deterministic values and cover eligible, too-recent, unavailable,
duplicate, and ambiguous legacy decisions:

```python
@dataclass(frozen=True)
class PreparedReflections:
    jobs: list[ReflectionJobRecord]
    warnings: list[str]


def reflection_service(tmp_path):
    config = {
        "memory_log_path": str(tmp_path / "memory.md"),
        "results_dir": str(tmp_path / "results"),
        "benchmark_ticker": None,
        "benchmark_map": {"": "SPY"},
    }
    store = PluginStore(tmp_path / "state")
    return WorkflowService(store, config), TradingMemoryLog(config)


def test_prepare_reflections_creates_one_reusable_job(tmp_path, monkeypatch):
    service, memory = reflection_service(tmp_path)
    memory.store_decision(
        "AAPL", "2026-01-05", "Buy with controlled sizing.", decision_id="decision-1"
    )
    monkeypatch.setattr(
        "tradingagents.plugin.workflow.calculate_outcome",
        lambda *_args, **_kwargs: Outcome(0.10, 0.05, 5, "SPY", "2026-01-10"),
    )
    first = service.prepare_reflections("AAPL", "2026-01-10")
    repeated = service.prepare_reflections("AAPL", "2026-01-10")
    assert [job.job_id for job in first.jobs] == [job.job_id for job in repeated.jobs]
    assert first.jobs[0].holding_sessions == 5


def test_prepare_reflections_skips_unavailable_outcome(tmp_path, monkeypatch):
    service, memory = reflection_service(tmp_path)
    memory.store_decision(
        "AAPL", "2026-01-05", "Buy with controlled sizing.", decision_id="decision-1"
    )
    monkeypatch.setattr(
        "tradingagents.plugin.workflow.calculate_outcome",
        lambda *_args, **_kwargs: None,
    )
    assert service.prepare_reflections("AAPL", "2026-01-10").jobs == []
    assert memory.load_entries()[0]["pending"] is True


def test_prepare_reflections_skips_resolution_after_cutoff(tmp_path, monkeypatch):
    service, memory = reflection_service(tmp_path)
    memory.store_decision(
        "AAPL", "2026-01-05", "Buy with controlled sizing.", decision_id="decision-1"
    )
    monkeypatch.setattr(
        "tradingagents.plugin.workflow.calculate_outcome",
        lambda *_args, **_kwargs: Outcome(0.10, 0.05, 5, "SPY", "2026-01-11"),
    )
    assert service.prepare_reflections("AAPL", "2026-01-10").jobs == []
    assert memory.load_entries()[0]["pending"] is True


def test_prepare_reflections_skips_ambiguous_legacy_identity(tmp_path, monkeypatch):
    service, memory = reflection_service(tmp_path)
    block = "[2026-01-05 | AAPL | Buy | pending]\n\nDECISION:\nBuy with controlled sizing."
    Path(memory._log_path).write_text(
        block + TradingMemoryLog._SEPARATOR + block + TradingMemoryLog._SEPARATOR,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tradingagents.plugin.workflow.calculate_outcome",
        lambda *_args, **_kwargs: Outcome(0.10, 0.05, 5, "SPY", "2026-01-10"),
    )
    prepared = service.prepare_reflections("AAPL", "2026-01-10")
    assert prepared.jobs == []
    assert any("ambiguous legacy identity" in warning for warning in prepared.warnings)
```

The service must independently verify `resolution_date <= as_of_date` even though the shared
calculator also applies the cutoff.

Add `from pathlib import Path`, `TradingMemoryLog`, and `Outcome` imports to the workflow test
module.

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/test_plugin_workflow.py -k prepare_reflections -q`

Expected: FAIL because preparation is absent.

- [ ] **Step 3: Implement preparation without model calls**

Return a `PreparedReflections(jobs, warnings)` dataclass. Validate the date before work. Read
pending same-ticker history, skip ambiguous derived identities, call `calculate_outcome` with the
server config and cutoff, re-read the target before insertion, then call
`create_reflection_job`. Never import or instantiate `Reflector`.

- [ ] **Step 4: Write failing lifecycle and race tests**

```python
@dataclass(frozen=True)
class ReflectionFinalization:
    run_id: str
    status: Literal["completed"]
    decision_id: str
    changed: bool
    warning: str | None


view = service.describe_work(job.job_id)
assert view.active_role == "reflection"
assert view.output_schema == {"type": "string", "minLength": 1, "maxLength": 32000}
accepted = service.submit_stage(job.job_id, "reflection", 1, "The call beat SPY. Keep the lesson.")
assert accepted.status == "ready_to_finalize"
done = service.finalize_analysis(job.job_id)
assert done.changed is True
assert memory.load_entries()[0]["reflection"] == "The call beat SPY. Keep the lesson."
```

For the race case, resolve memory through the legacy/API path after job preparation, then finalize
the plugin job and assert `changed is False`, warning is `ALREADY_RESOLVED`, and the first
reflection remains unchanged.

- [ ] **Step 5: Implement reflection dispatch**

When an ID is not an analysis run, load a reflection job. `get_analysis` returns a strict
`ReflectionResult`; `submit_stage` validates `stage_id == "reflection"`, a nonblank string, and the
existing 32,000-character bound; `finalize_analysis` calls `resolve_decision` and completes the job.
Convert `build_reflection_messages` tuples to `[{'role': role, 'content': content}]` for the public
instructions field. Missing/ambiguous decisions remain retryable errors; `already_resolved`
completes with `changed=false`.

Add `work_type: Literal["analysis"] = "analysis"` to `AnalysisResult` and define the reflection
models with these exact fields:

```python
class ReflectionOutcomeResult(BaseModel):
    raw_return: float
    alpha_return: float
    holding_sessions: Literal[5]
    benchmark: str
    resolution_date: str


class ReflectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    work_type: Literal["reflection"] = "reflection"
    status: Literal["active", "ready_to_finalize", "completed"]
    revision: int
    current_stage: str
    decision_id: str
    ticker: str
    decision_date: str
    decision: str
    outcome: ReflectionOutcomeResult
    active_role: Literal["reflection"] | None
    instructions: list[dict[str, str]] | None
    output_schema: dict | None
    reflection: str | None


class PreparedReflectionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jobs: list[ReflectionResult]
    warnings: list[str]


class ReflectionSubmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    work_type: Literal["reflection"] = "reflection"
    status: Literal["ready_to_finalize"]
    revision: int
    current_stage: Literal["finalize"]
    reflection: str


class ReflectionFinalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    work_type: Literal["reflection"] = "reflection"
    status: Literal["completed"]
    revision: int
    current_stage: Literal["completed"]
    decision_id: str
    changed: bool
    warning: str | None
```

Return `AnalysisResult | ReflectionResult` from `get_analysis`,
`SubmissionResult | ReflectionSubmissionResult` from `submit_stage`, and
`AnalysisFinalizationResult | ReflectionFinalizationResult` from `finalize_analysis`; the
reflection variants retain the shared `run_id`, `status`, `revision`, and `current_stage` fields.

- [ ] **Step 6: Add a no-LLM regression**

Patch `tradingagents.llm_clients.factory.create_llm_client` and `Reflector` construction to raise;
run prepare/read/submit/finalize and assert completion. This pins the Codex-only reasoning boundary.

- [ ] **Step 7: Run reflection tests**

Run: `pytest tests/test_plugin_workflow.py tests/test_plugin_data.py -k reflection -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add tradingagents/plugin/workflow.py tradingagents/plugin/data.py tests/test_plugin_workflow.py tests/test_plugin_data.py
git commit -m "feat(plugin): add durable Codex reflection jobs"
```

### Task 6: Expose point-in-time history, omission state, and capabilities

**Files:**
- Modify: `tradingagents/plugin/data.py`
- Modify: `tradingagents/plugin/server.py`
- Test: `tests/test_plugin_data.py`
- Test: `tests/test_plugin_capabilities.py`
- Test: `tests/test_plugin_runtime.py`

**Interfaces:**
- Produces public `get_decision_history(ticker=None, as_of_date=None, limit=20, cursor=None)`.
- Extends `start_analysis` with `skip_reflections: bool = False` and normalized input `learning_omitted`.
- Advertises schema version 3 and makes reflection/finalization/history tools available.

- [ ] **Step 1: Write failing history API tests**

```python
def test_history_filters_ticker_pages_and_hides_future_outcome(tmp_path):
    config = {"memory_log_path": str(tmp_path / "memory.md")}
    memory = TradingMemoryLog(config)
    plugin_tools = data.PluginTools(PluginStore(tmp_path / "state"), server_config=config)
    memory.store_decision("AAPL", "2026-01-05", "First", decision_id="aapl-1")
    memory.resolve_decision(
        decision_id="aapl-1", ticker="AAPL", trade_date="2026-01-05",
        raw_return=0.10, alpha_return=0.05, holding_days=5, benchmark_name="SPY",
        resolution_date="2026-01-10", reflection="Future lesson.",
    )
    memory.store_decision("AAPL", "2026-01-06", "Second", decision_id="aapl-2")
    memory.store_decision("MSFT", "2026-01-06", "Other", decision_id="msft-1")
    first = plugin_tools.get_decision_history(
        ticker="AAPL", as_of_date="2026-01-09", limit=1
    )
    assert len(first.entries) == 1
    assert first.entries[0].ticker == "AAPL"
    assert first.entries[0].pending is True
    assert first.entries[0].reflection is None
    second = plugin_tools.get_decision_history(
        ticker="AAPL", as_of_date="2026-01-09", limit=1, cursor=first.next_cursor
    )
    assert {first.entries[0].decision_id, second.entries[0].decision_id} == {
        "aapl-1", "aapl-2"
    }


def test_history_rejects_path_like_unknown_arguments(tools):
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        tools.get_decision_history(path="/tmp/secret")
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/test_plugin_data.py -k history -q`

Expected: FAIL because the tool/models do not exist.

- [ ] **Step 3: Implement bounded opaque paging**

Validate ticker/date with existing helpers and limit with `_validate_limit`. Encode the last
decision identity plus its zero-based occurrence among identical derived identities as URL-safe
base64 JSON using the existing cursor style. Read only the configured memory object, newest first.
A cursor not found in the filtered projection raises `ValueError` instead of restarting at page
one. Map empty internal strings to optional public fields.

Define strict `DecisionHistoryEntry` and `DecisionHistoryResult` models. The entry fields are
`decision_id`, `decision_date`, `ticker`, `rating`, `pending`, `decision`, optional `raw_return`,
optional `alpha_return`, optional `holding_sessions`, optional `benchmark`, optional
`resolution_date`, and optional `reflection`; the result fields are `entries`, `warnings`, and
`next_cursor`.

- [ ] **Step 4: Write and implement omission-state tests**

Add `skip_reflections` to `StartAnalysisRequest`, the method signature, fingerprint, and normalized
inputs:

```python
result = tools.start_analysis(request_id="request-1", ticker="AAPL", skip_reflections=True)
assert result.learning_omitted is True
assert store.get_run(result.run_id).normalized_inputs["learning_omitted"] is True
with pytest.raises(RequestIdConflict):
    tools.start_analysis(request_id="request-1", ticker="AAPL", skip_reflections=False)
```

Default false must preserve existing callers.

- [ ] **Step 5: Update capability and protocol expectations**

Set capability `schema_version=3`, mark reflection `workflow_available=True`, move
`get_decision_history` out of planned tools, and add `finalize_analysis`, `prepare_reflections`, and
`get_decision_history` to every expected tool set. Assert every new schema has
`additionalProperties: false` and no path/destination property.

- [ ] **Step 6: Run public contract tests**

Run: `pytest tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/plugin/data.py tradingagents/plugin/server.py tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py
git commit -m "feat(plugin): expose history and learning lifecycle"
```

### Task 7: Document and verify US-014–016 end to end

**Files:**
- Modify: `docs/plugin-runtime.md`
- Create: `docs/user-testing-us-014-016.md`
- Modify: `tasks/prd-tradingagents-codex-plugin-mcp.md`

**Interfaces:**
- Consumes: completed public contracts from Tasks 1–6.
- Produces: exact runtime lifecycle and verification evidence; no code interface.

- [ ] **Step 1: Run focused regression suites**

Run:

```bash
pytest tests/test_reporting.py tests/test_memory_log.py tests/test_memory_pointintime.py -q
pytest tests/test_plugin_store.py tests/test_plugin_workflow.py tests/test_plugin_data.py -q
pytest tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q
```

Expected: all pass.

- [ ] **Step 2: Run full verification**

Run:

```bash
pytest -q
ruff check .
python -m pip wheel . --no-deps -w /tmp/tradingagents-dist
```

Expected: all tests pass, Ruff reports no errors, and the wheel builds successfully.

- [ ] **Step 3: Update runtime documentation**

Document:

- fixed run-owned artifact paths and finalization retry ordering;
- canonical Markdown identity and cross-process locking;
- history filters and true point-in-time projection;
- fixed five-session outcomes and benchmark identity;
- reflection job read/submit/finalize flow;
- `skip_reflections`/`learning_omitted`; and
- the explicit US-017 boundary for automatic pre-analysis orchestration.

- [ ] **Step 4: Record the user-testing checklist**

Create `docs/user-testing-us-014-016.md` with the commands from Steps 1–2, dated result slots,
and manual checks for restart after memory export, two-process history preservation, legacy entry
reflection, history cutoff, and installed stdio discovery. Fill each automated result with the
actual command output summary; do not mark an unperformed manual check complete.

- [ ] **Step 5: Update PRD acceptance boxes only from evidence**

Mark US-014 and US-015 criteria complete when their focused/full checks pass. For US-016, mark the
runtime/reflection criteria complete but leave the automatic-skill-orchestration criterion open
with a note that it is intentionally delivered by US-017, matching the approved spec.

- [ ] **Step 6: Check the final diff and commit**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only scoped implementation, tests, and documentation are changed.

```bash
git add docs/plugin-runtime.md docs/user-testing-us-014-016.md tasks/prd-tradingagents-codex-plugin-mcp.md
git commit -m "docs: verify plugin artifacts and reflection"
```

- [ ] **Step 7: Request final code review**

Use `superpowers:requesting-code-review` for the complete branch. The reviewer must specifically
check external-file retry boundaries, legacy identity ambiguity, cross-process locking, point-in-
time leakage, absence of plugin LLM construction, and the deliberate US-017 orchestration deferral.
