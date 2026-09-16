# Codex Plugin Evidence and Run Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement US-005–US-008 as typed MCP evidence tools plus restart-safe, idempotent analysis-run and evidence storage.

**Architecture:** Preserve existing API-runner behavior by adding an opt-in traced vendor result beneath `route_to_vendor()`. A concrete `PluginStore` owns SQLite transactions; a concrete `PluginTools` adapter validates MCP inputs, scopes the process-global data configuration, calls existing dataflows, and persists terminal run-bound evidence. Only the first selected analyst stage exists in this milestone.

**Tech Stack:** Python 3.10+, Pydantic v2, stdlib `sqlite3`/`threading`/`uuid`/`hashlib`/`base64`, FastMCP 1.26.0, pytest, Ruff.

**Spec:** [`docs/superpowers/specs/2026-09-15-codex-plugin-evidence-runs-design.md`](../specs/2026-09-15-codex-plugin-evidence-runs-design.md)

## Global Constraints

- Preserve Python 3.10–3.13 support and add no dependency.
- Preserve `route_to_vendor()` content/exception behavior for every existing API-runner caller.
- Never construct an LLM client or use an LLM to summarize, repair, or replace source data.
- Do not accept credentials, model settings, storage paths, arbitrary function names, Python code, or filesystem destinations through MCP tools.
- Store plugin state below the startup-owned state root; stdout remains MCP protocol-only.
- Run-bound evidence calls use explicit ticker/date arguments and the run's frozen settings.
- Persist `success`, `no_data`, and terminal `unavailable`; never persist retryable `error` results.
- Use a 32,000-character maximum evidence page and round counts from 1 through 10.
- Build only the first analyst stage. Do not add submission, advancement, cancellation, export, history, reflection, skill, or packaging behavior.

## File map

- Modify `tradingagents/dataflows/config.py`: exact deep-copy replacement alongside existing merge semantics.
- Modify `tradingagents/dataflows/interface.py`: `VendorRouteResult` and traced routing; legacy wrapper remains compatible.
- Create `tradingagents/plugin/store.py`: versioned SQLite schema, run idempotency, evidence uniqueness, and restart reads.
- Create `tradingagents/plugin/data.py`: public models, lifecycle validation, evidence executor, paging, permissions, and fifteen typed methods.
- Modify `tradingagents/plugin/server.py`: construct store/tools from the state root and register all public operations.
- Modify `tests/test_dataflows_config.py` and `tests/test_vendor_routing.py`: shared primitive regressions.
- Create `tests/test_plugin_store.py`: persistence, idempotency, schema compatibility, and stale-write checks.
- Create `tests/test_plugin_data.py`: lifecycle, wrappers, isolation, classification, reuse, and paging.
- Modify `tests/test_plugin_capabilities.py`, `tests/test_plugin_runtime.py`, and `scripts/smoke_plugin_protocol.py`: discovery and installed protocol coverage.
- Modify `docs/plugin-runtime.md` and `docs/user-testing-us-005-008.md`: describe the implemented surface and remove the pre-implementation warning.

---

### Task 1: Exact configuration replacement and traced vendor routing

**Files:**
- Modify: `tradingagents/dataflows/config.py`
- Modify: `tradingagents/dataflows/interface.py`
- Test: `tests/test_dataflows_config.py`
- Test: `tests/test_vendor_routing.py`

**Interfaces:**
- Produces: `replace_config(config: dict) -> None`
- Produces: `VendorRouteResult(content, status, source, warnings, retryable, legacy_error)`
- Produces: `route_to_vendor_traced(method: str, *args, **kwargs) -> VendorRouteResult`
- Preserves: `route_to_vendor(method: str, *args, **kwargs)`

- [ ] **Step 1: Add failing exact-replacement tests**

Add to `tests/test_dataflows_config.py`:

```python
from tradingagents.dataflows.config import get_config, replace_config, set_config


def test_replace_config_clears_stale_nested_keys():
    set_config({"tool_vendors": {"get_news": "alpha_vantage"}})
    replacement = {"data_vendors": {"news_data": "yfinance"}, "tool_vendors": {}}

    replace_config(replacement)
    replacement["data_vendors"]["news_data"] = "alpha_vantage"

    assert get_config() == {
        "data_vendors": {"news_data": "yfinance"},
        "tool_vendors": {},
    }
```

- [ ] **Step 2: Add failing trace and compatibility tests**

Add focused cases to `tests/test_vendor_routing.py`:

```python
def test_trace_reports_actual_fallback_vendor_and_warning(self):
    set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
    with self._route({
        "yfinance": _raises(RuntimeError("primary failed")),
        "alpha_vantage": _returns("AV_DATA"),
    }):
        traced = interface.route_to_vendor_traced(
            "get_stock_data", "AAPL", "2026-01-01", "2026-01-10"
        )
        legacy = interface.route_to_vendor(
            "get_stock_data", "AAPL", "2026-01-01", "2026-01-10"
        )

    self.assertEqual(traced.content, "AV_DATA")
    self.assertEqual(traced.status, "success")
    self.assertEqual(traced.source, "alpha_vantage")
    self.assertTrue(any("yfinance" in warning for warning in traced.warnings))
    self.assertEqual(legacy, "AV_DATA")


def test_trace_classifies_missing_only_vendor_without_changing_legacy_raise(self):
    missing = interface.VendorNotConfiguredError("missing key")
    set_config({"data_vendors": {"core_stock_apis": "alpha_vantage"}})
    with self._route({"alpha_vantage": _raises(missing)}):
        traced = interface.route_to_vendor_traced(
            "get_stock_data", "AAPL", "2026-01-01", "2026-01-10"
        )
        self.assertEqual(traced.status, "unavailable")
        self.assertFalse(traced.retryable)
        with self.assertRaises(interface.VendorNotConfiguredError):
            interface.route_to_vendor(
                "get_stock_data", "AAPL", "2026-01-01", "2026-01-10"
            )
```

- [ ] **Step 3: Run the new tests and verify the missing APIs fail**

Run:

```bash
pytest tests/test_dataflows_config.py::test_replace_config_clears_stale_nested_keys \
  tests/test_vendor_routing.py::VendorRoutingTests::test_trace_reports_actual_fallback_vendor_and_warning \
  tests/test_vendor_routing.py::VendorRoutingTests::test_trace_classifies_missing_only_vendor_without_changing_legacy_raise -q
```

Expected: collection/import failures for `replace_config` and `route_to_vendor_traced`.

- [ ] **Step 4: Implement exact replacement**

Add to `tradingagents/dataflows/config.py`:

```python
def replace_config(config: dict) -> None:
    """Replace the complete active configuration without aliasing caller data."""
    global _config
    _config = deepcopy(config)
```

Do not change `set_config()`.

- [ ] **Step 5: Implement traced routing and legacy delegation**

Add this public result type to `tradingagents/dataflows/interface.py`:

```python
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class VendorRouteResult:
    content: object
    status: Literal["success", "no_data", "unavailable", "error"]
    source: str | None
    warnings: Sequence[str] = ()
    retryable: bool = False
    legacy_error: Exception | None = field(default=None, repr=False, compare=False)
```

Move the current vendor-chain loop into `route_to_vendor_traced()`. Accumulate one warning per
failed vendor using `"{vendor}: {type(exc).__name__}: {exc}"`. Return these exact outcomes:

| Final condition | status | source | retryable | legacy_error |
|---|---|---|---|---|
| vendor returns content | `success` | that vendor | false | null |
| at least one `NoMarketDataError`, no success | `no_data` | null | false | null |
| only `VendorNotConfiguredError` failures | `unavailable` | null | false | first error for core categories |
| exhausted rate limits or other exceptions | `error` | null | true | first error for core categories |
| optional-category operational failure | `error` | null | true | null |

Reuse the current `NO_DATA_AVAILABLE` and optional `DATA_UNAVAILABLE` string construction verbatim.
Then reduce the legacy wrapper to:

```python
def route_to_vendor(method: str, *args, **kwargs):
    result = route_to_vendor_traced(method, *args, **kwargs)
    if result.legacy_error is not None:
        raise result.legacy_error
    return result.content
```

- [ ] **Step 6: Run routing/config regressions and lint**

Run:

```bash
pytest tests/test_dataflows_config.py tests/test_vendor_routing.py \
  tests/test_vendor_errors.py tests/test_no_data_handling.py -q
ruff check tradingagents/dataflows/config.py tradingagents/dataflows/interface.py \
  tests/test_dataflows_config.py tests/test_vendor_routing.py
```

Expected: all pass; existing legacy routing tests remain unchanged.

- [ ] **Step 7: Commit the shared primitives**

```bash
git add tradingagents/dataflows/config.py tradingagents/dataflows/interface.py \
  tests/test_dataflows_config.py tests/test_vendor_routing.py
git commit -m "feat(dataflows): expose traced vendor results"
```

---

### Task 2: Versioned SQLite run and evidence store

**Files:**
- Create: `tradingagents/plugin/store.py`
- Create: `tests/test_plugin_store.py`

**Interfaces:**
- Produces: immutable `RunRecord` and `EvidenceRecord` dataclasses
- Produces: `PluginStore(state_root: str | Path)`
- Produces: `create_run`, `get_run`, `find_evidence`, `save_evidence`, `list_evidence`
- Produces: `RequestIdConflict`, `RunNotFound`, `StaleRun`, `IncompatibleState`

- [ ] **Step 1: Write failing schema, restart, and request-id tests**

Create `tests/test_plugin_store.py` with fixtures that call the exact interface:

```python
import pytest

from tradingagents.plugin.store import PluginStore, RequestIdConflict


def run_values(request_hash="hash-one"):
    return dict(
        request_id="11111111-1111-4111-8111-111111111111",
        request_hash=request_hash,
        normalized_inputs={"ticker": "AAPL"},
        current_stage="analyst/market",
        frozen_config={"data_vendors": {"core_stock_apis": "yfinance"}},
        instrument={"requested_symbol": "AAPL", "canonical_symbol": "AAPL"},
        lessons="past lesson",
        now="2026-09-15T12:00:00+00:00",
    )


def test_create_run_is_idempotent_and_survives_restart(tmp_path):
    store = PluginStore(tmp_path)
    first, first_created = store.create_run(**run_values())
    repeated, repeated_created = store.create_run(**run_values())
    reopened = PluginStore(tmp_path).get_run(first.run_id)

    assert first_created is True
    assert repeated_created is False
    assert repeated.run_id == first.run_id
    assert reopened == first
    assert first.status == "active"
    assert first.revision == 1


def test_changed_request_payload_conflicts_without_second_run(tmp_path):
    store = PluginStore(tmp_path)
    store.create_run(**run_values())

    with pytest.raises(RequestIdConflict, match="REQUEST_ID_CONFLICT"):
        store.create_run(**run_values(request_hash="hash-two"))
```

- [ ] **Step 2: Write failing evidence uniqueness and stale-stage tests**

Add:

```python
from tradingagents.plugin.store import StaleRun


def test_evidence_is_unique_and_stale_stage_is_rejected(tmp_path):
    store = PluginStore(tmp_path)
    run, _ = store.create_run(**run_values())
    values = dict(
        run_id=run.run_id,
        expected_stage="analyst/market",
        tool_name="get_stock_data",
        argument_hash="args-hash",
        arguments={"symbol": "AAPL", "start_date": "2026-09-01", "end_date": "2026-09-15"},
        status="success",
        fetched_at="2026-09-15T12:01:00+00:00",
        requested_window={"start": "2026-09-01", "end": "2026-09-15"},
        content="prices",
        content_format="text",
        source={"vendor": "yfinance"},
        warnings=[],
    )

    first, created = store.save_evidence(**values)
    repeated, repeated_created = store.save_evidence(**values)

    assert created is True
    assert repeated_created is False
    assert repeated.evidence_id == first.evidence_id
    assert store.find_evidence(
        run.run_id, "analyst/market", "get_stock_data", "args-hash"
    ) == first
    with pytest.raises(StaleRun, match="STALE_RUN"):
        store.save_evidence(**{**values, "expected_stage": "analyst/news"})
```

- [ ] **Step 3: Run the store tests and verify the module is missing**

Run: `pytest tests/test_plugin_store.py -q`

Expected: import failure for `tradingagents.plugin.store`.

- [ ] **Step 4: Implement records, errors, and schema**

Create `tradingagents/plugin/store.py` with `SCHEMA_VERSION = 1`, frozen dataclasses matching the
test fields, and `PluginStore.__init__` that creates `<state_root>/plugin.sqlite3`. Every connection
must set `row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`, and `PRAGMA busy_timeout=5000`.
Initialization sets WAL mode and creates:

```sql
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    normalized_inputs_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','ready_to_finalize','completed','cancelled')),
    revision INTEGER NOT NULL,
    current_stage TEXT NOT NULL,
    frozen_config_json TEXT NOT NULL,
    instrument_json TEXT NOT NULL,
    lessons TEXT NOT NULL,
    state_schema INTEGER NOT NULL,
    prompt_schema INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    stage_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    argument_hash TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success','no_data','unavailable')),
    fetched_at TEXT NOT NULL,
    requested_window_json TEXT NOT NULL,
    content TEXT NOT NULL,
    content_format TEXT NOT NULL CHECK (content_format IN ('text','json')),
    source_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    UNIQUE (run_id, stage_id, tool_name, argument_hash)
);
```

Insert `metadata('schema_version','1')`; if an existing value differs, raise
`IncompatibleState("INCOMPATIBLE_STATE: database schema 999 is not supported; expected 1")`
without running DDL mutations.
Every new run stores `state_schema=1` and `prompt_schema=1`, and `RunRecord` exposes both fields.

- [ ] **Step 5: Implement transactional store methods**

Use canonical JSON (`sort_keys=True`, `separators=(",", ":")`) and UUID4 strings. `create_run()`
uses `BEGIN IMMEDIATE`, checks `request_id`, returns the existing row on equal hash, and raises
`RequestIdConflict` otherwise. `save_evidence()` uses `BEGIN IMMEDIATE`, verifies active status and
the exact `expected_stage`, performs `INSERT OR IGNORE`, and returns the winning row. It never
changes `runs.revision`. `list_evidence(run_id)` orders by `fetched_at, evidence_id`.

Define the public signatures exactly as follows:

- `create_run(*, request_id: str, request_hash: str, normalized_inputs: dict, current_stage: str, frozen_config: dict, instrument: dict, lessons: str, now: str) -> tuple[RunRecord, bool]`
- `get_run(run_id: str) -> RunRecord`
- `find_evidence(run_id: str, stage_id: str, tool_name: str, argument_hash: str) -> EvidenceRecord | None`
- `save_evidence(*, run_id: str, expected_stage: str, tool_name: str, argument_hash: str, arguments: dict, status: str, fetched_at: str, requested_window: dict, content: str, content_format: str, source: dict, warnings: list[str]) -> tuple[EvidenceRecord, bool]`
- `list_evidence(run_id: str) -> list[EvidenceRecord]`

Expose the resolved SQLite file as the read-only `database_path: Path` attribute so tests can
verify that rejected requests did not insert rows without adding a production-only list method.

- [ ] **Step 6: Add and pass incompatible-schema and unknown-run tests**

Add tests that change `metadata.schema_version` to `999`, assert reopening raises
`IncompatibleState`, and assert the database still contains `999`. Assert `get_run()` and
`list_evidence()` raise `RunNotFound("RUN_NOT_FOUND: analysis <uuid> does not exist")` for an
unknown UUID.

Run:

```bash
pytest tests/test_plugin_store.py -q
ruff check tradingagents/plugin/store.py tests/test_plugin_store.py
```

Expected: all pass.

- [ ] **Step 7: Commit the durable store**

```bash
git add tradingagents/plugin/store.py tests/test_plugin_store.py
git commit -m "feat(plugin): persist analysis runs and evidence"
```

---

### Task 3: Validated run creation and read-only analysis status

**Files:**
- Create: `tradingagents/plugin/data.py`
- Create: `tests/test_plugin_data.py`

**Interfaces:**
- Consumes: `PluginStore`, `RunRecord`, existing analyst metadata/symbol/memory helpers
- Produces: `VendorOverrides`, `StartAnalysisRequest`, `AnalysisResult`, `EvidenceMetadata`, `PluginTools.start_analysis`, `PluginTools.get_analysis`
- Produces: canonical helpers `_canonical_json`, `_digest`, `_parse_date`, `_validate_ticker`

- [ ] **Step 1: Write failing lifecycle tests**

Create `tests/test_plugin_data.py` with a `PluginTools(PluginStore(tmp_path))` fixture. Monkeypatch
`data.resolve_instrument_identity` to return deterministic metadata and `data.get_current_date` to
return `"2026-09-15"`. Assert:

```python
def test_start_analysis_defaults_and_freezes_first_stage(tools, monkeypatch):
    monkeypatch.setattr(data, "resolve_instrument_identity", lambda ticker: {"company_name": "Apple"})
    monkeypatch.setattr(data, "get_current_date", lambda: "2026-09-15")

    result = tools.start_analysis(
        request_id="11111111-1111-4111-8111-111111111111",
        ticker="aapl",
        asset_type="stock",
    )

    assert result.status == "active"
    assert result.revision == 1
    assert result.current_stage == "analyst/market"
    assert result.analysis_date == "2026-09-15"
    assert result.analysts == ["market", "social", "news", "fundamentals"]
    assert result.instrument["canonical_symbol"] == "AAPL"
    assert "llm_provider" not in result.frozen_config
```

Also assert same UUID + normalized inputs returns the same run, changed inputs raise
`REQUEST_ID_CONFLICT`, and `get_analysis(run_id)` returns identical frozen fields without invoking
the identity resolver again.

- [ ] **Step 2: Write failing boundary-validation tests**

Parametrize invalid request UUID, ticker, calendar date, asset type, empty/duplicate/unknown
analysts, rounds `0` and `11`, empty language, unknown category vendor, unknown tool override, and a
vendor unavailable for the chosen tool. Query `store.database_path` with
`SELECT count(*) FROM runs` in a small test helper and assert zero after every rejection.

- [ ] **Step 3: Run lifecycle tests and verify missing types/methods**

Run: `pytest tests/test_plugin_data.py -q`

Expected: import or attribute failures for `PluginTools` and response models.

- [ ] **Step 4: Implement public lifecycle models and constants**

In `tradingagents/plugin/data.py`, add strict Pydantic models (`ConfigDict(extra="forbid")`):

```python
AnalystKey = Literal["market", "social", "news", "fundamentals"]
AssetType = Literal["stock", "crypto"]
MAX_ROUNDS = 10
MAX_PAGE_SIZE = 32_000


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
```

- [ ] **Step 5: Implement normalization and frozen configuration**

Initialize the concrete adapter with:

```python
class PluginTools:
    def __init__(self, store: PluginStore, server_config: dict | None = None):
        self._store = store
        self._server_config = deepcopy(server_config if server_config is not None else get_config())
```

Use `safe_ticker_component()` on the trimmed input and `normalize_symbol()` for the canonical
ticker. Parse dates only with `date.fromisoformat`. Validate UUIDs with `UUID(value)` and store the
canonical string. Validate vendor chains by splitting commas, rejecting blanks/duplicates, and
requiring each vendor in the category/tool's `VENDOR_METHODS` entries; allow `default` only alone.
Add a `StartAnalysisRequest` model validator that replaces `analysts=None` with
`list(ANALYST_NODE_SPECS)`, rejects an empty or duplicate selection, strips language whitespace,
and canonicalizes the request UUID. Construct this model at the start of the public method so
direct Python calls receive the same validation as MCP calls.

Build `frozen_config` from the server's current config using only:

```python
DATA_CONFIG_KEYS = (
    "data_cache_dir", "news_article_limit", "global_news_article_limit",
    "global_news_lookback_days", "global_news_queries", "data_vendors", "tool_vendors",
)
```

Then set `output_language`, `max_debate_rounds`, and `max_risk_discuss_rounds`. Apply only validated
vendor overrides. Never copy LLM, results, checkpoint, or memory-path keys into the stored value.

- [ ] **Step 6: Implement `start_analysis` and `get_analysis`**

Define the explicit public method:

```python
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
```

Preserve explicit analyst order; use `list(ANALYST_NODE_SPECS)` only when `analysts is None`.
Resolve identity once, build context with `build_instrument_context(canonical, asset_type, metadata)`,
and store:

```python
instrument = {
    "requested_symbol": ticker.strip().upper(),
    "canonical_symbol": canonical,
    "asset_type": asset_type,
    "metadata": metadata,
    "context": context,
    "source": "yfinance" if metadata else "symbol_utils",
}
```

Read lessons from `TradingMemoryLog(self._server_config).get_past_context(canonical, as_of=date)`.
Hash canonical normalized inputs with SHA-256. Call `store.create_run()` with stage
`f"analyst/{analysts[0]}"`. Convert records through one `_analysis_result()` helper used by both
`start_analysis` and:

```python
def get_analysis(self, run_id: str) -> AnalysisResult:
```

`get_analysis` calls only `store.get_run()` and `store.list_evidence()`.

For idempotency, hash a request fingerprint containing the normalized ticker, the caller's
`analysis_date` value (including null when omitted), asset type, ordered analysts, rounds, language,
and canonical vendor overrides. Store separate normalized run inputs containing the resolved
analysis date. This ensures a retry with an omitted date returns the original run even after the
host calendar advances, while a changed explicit date conflicts.

- [ ] **Step 7: Run lifecycle/store tests and lint**

Run:

```bash
pytest tests/test_plugin_store.py tests/test_plugin_data.py -q
ruff check tradingagents/plugin/data.py tests/test_plugin_data.py
```

Expected: lifecycle tests pass; no network or LLM client is invoked.

- [ ] **Step 8: Commit run lifecycle support**

```bash
git add tradingagents/plugin/data.py tests/test_plugin_data.py
git commit -m "feat(plugin): create persistent configured analyses"
```

---

### Task 4: Common evidence executor, paging, and configuration isolation

**Files:**
- Modify: `tradingagents/plugin/data.py`
- Modify: `tests/test_plugin_data.py`

**Interfaces:**
- Consumes: `route_to_vendor_traced`, `replace_config`, `PluginStore`
- Produces: `EvidencePage`, `DataToolResult`, `_execute`, `_page_record`, `_encode_cursor`, `_decode_cursor`

- [ ] **Step 1: Write failing reuse, retry, and paging tests**

Use a fake fetch callable with a call counter and call the private `_execute()` directly; it is the
unit under test and avoids a test-only production method. Assert the first terminal call has
`reused=false`, the identical second call has `reused=true`, both share evidence
ID/fetch time/content, and the counter remains one.

Add a retryable `VendorRouteResult(status="error", retryable=True)` case and assert two calls invoke
the fetch twice and both have `evidence_id is None`. Add 1,200 characters with page size 500 and
assert page lengths `500, 500, 200`, valid continuation, malformed-cursor rejection, and rejection
when cursor arguments change.

- [ ] **Step 2: Write failing permission, date, and restoration tests**

Create a market run and assert a news tool fails before its fake fetch runs. Assert mismatched
ticker, point date, reversed range, and range end after analysis date fail before fetch.

For isolation, use two threads, two runs with different `tool_vendors`, and a barrier inside fake
fetches. Record `get_config()` observed by each fetch; make one raise. Assert each sees its own full
frozen config and the exact pre-test config is restored after both calls. Because the global lock
serializes calls, use the barrier only between the test thread and the active fetch, never between
the two fetches.

- [ ] **Step 3: Run focused tests and verify failures**

Run: `pytest tests/test_plugin_data.py -k 'reuse or retry or paging or permission or restoration' -q`

Expected: failures for missing result models/executor.

- [ ] **Step 4: Implement result and page models**

```python
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
```

Serialize strings unchanged; serialize mappings/lists using canonical JSON and set format `json`.
Use timezone-aware UTC ISO timestamps.

- [ ] **Step 5: Implement permissions, cursors, and `_execute`**

Define the exact permission map:

```python
STAGE_TOOLS = {
    "analyst/market": {"get_stock_data", "get_indicators", "get_verified_market_snapshot"},
    "analyst/social": {"get_news", "fetch_stocktwits_messages", "fetch_reddit_posts"},
    "analyst/news": {
        "get_news", "get_global_news", "get_insider_transactions",
        "get_macro_indicators", "get_prediction_markets",
    },
    "analyst/fundamentals": {
        "get_fundamentals", "get_balance_sheet", "get_cashflow",
        "get_income_statement",
    },
}
```

Encode cursor JSON `{"evidence_id": id, "offset": integer}` with URL-safe Base64 and strict
decoding. `_execute(tool_name, arguments, requested_window, fetch, run_id, cursor, page_size)` does:

1. Reject cursor/page size on standalone calls; lock, fetch, classify, and return complete content.
2. For run calls, load active run, enforce permission/ticker/date rules, hash arguments, and handle
   continuation from the matching saved record.
3. Return an existing record before locking; recheck after locking.
4. Snapshot `get_config()`, `replace_config(run.frozen_config)`, fetch, and restore in `finally`.
5. Return retryable errors without saving.
6. Save terminal results with `expected_stage`; page the winning record and set `reused` from the
   store's `created` boolean.

Instrument identity bypasses `STAGE_TOOLS`: standalone resolves it, while run-bound calls return
the frozen identity and never persist evidence.

- [ ] **Step 6: Implement deterministic content classification**

Start with the traced status, then recognize existing fixed-source strings without changing them:

- `NO_DATA_AVAILABLE`, `No news found`, `No global news found`, `No open prediction markets`,
  and complete-source `<no Reddit posts` results → `no_data`.
- Missing-key traced results and StockTwits' explicit `public stream serves only recent messages`
  historical limitation → `unavailable`.
- `Error fetching`, `currently unavailable (network error`, `<stocktwits unavailable`, and
  `<Reddit unavailable: every source failed` → retryable `error`.
- Reddit content containing one or more failed subreddits while other subreddits completed →
  `success` with warnings, even when the completed sources found no posts.

Do not parse arbitrary prose beyond these existing sentinel prefixes/substrings.

- [ ] **Step 7: Run evidence-engine tests and lint**

Run:

```bash
pytest tests/test_plugin_data.py tests/test_plugin_store.py -q
ruff check tradingagents/plugin/data.py tests/test_plugin_data.py
```

Expected: all pass, including restoration after the failing fetch.

- [ ] **Step 8: Commit the evidence engine**

```bash
git add tradingagents/plugin/data.py tests/test_plugin_data.py
git commit -m "feat(plugin): bind and page run evidence"
```

---

### Task 5: Expose all market, fundamental, contextual, and social methods

**Files:**
- Modify: `tradingagents/plugin/data.py`
- Modify: `tests/test_plugin_data.py`

**Interfaces:**
- Consumes: `_execute`, `route_to_vendor_traced`, existing verified snapshot/social/identity functions
- Produces: fifteen explicitly typed `PluginTools` methods named exactly as the MCP tools

- [ ] **Step 1: Add failing coverage for the US-005 methods**

Parametrize `get_stock_data`, `get_indicators`, `get_verified_market_snapshot`, fundamentals,
balance sheet, cash flow, and income statement. Patch `data.route_to_vendor_traced` or the verified
snapshot builder, call the public method, and assert exact argument forwarding and envelope fields.

Add identity cases for `CNC.TO`, `XAUUSD+ -> GC=F`, and `BTCUSD -> BTC-USD`. Add a routed
`NoMarketDataError` case that returns `status=no_data` with the existing no-data content.

- [ ] **Step 2: Add failing coverage for the US-006 methods**

Parametrize ticker/global news, insider transactions, macro indicators, prediction markets,
StockTwits, and Reddit. Assert date-window forwarding and source names. Cover missing FRED as
terminal unavailable, all-social-source failure as retryable error, partial Reddit as success with
warnings, and historical insider/prediction calls with a coverage warning.

- [ ] **Step 3: Run method coverage and verify missing methods fail**

Run: `pytest tests/test_plugin_data.py -k 'market or fundamental or contextual or social or identity' -q`

Expected: missing-attribute failures for unimplemented public methods.

- [ ] **Step 4: Implement the twelve existing financial methods**

Give every method its current domain arguments plus keyword-only `run_id=None`, `cursor=None`, and
`page_size=None`. Each method builds an argument dict from domain arguments only and calls
`_execute` with a lambda invoking `route_to_vendor_traced()` using the existing positional order.
Reject a comma-separated `indicator`; the public schema describes one indicator per call, and this
keeps one evidence record tied to one routed source result.

`get_verified_market_snapshot` instead calls `build_verified_market_snapshot`; convert
`NoMarketDataError` to the existing no-data text and other exceptions to retryable errors. Report
source `unknown` because the current snapshot API does not prove its backing vendor.

Use these date rules:

- `curr_date` methods require exact run analysis date.
- `start_date`/`end_date` methods require real ordered dates and `end_date <= analysis_date`.
- No-date methods receive the historical-coverage warning when `analysis_date < get_current_date()`.

- [ ] **Step 5: Implement identity, StockTwits, and Reddit methods**

`resolve_instrument_identity(ticker, run_id=None)` validates the ticker. With a run, require the
canonical ticker and return the frozen `instrument` JSON and its stored source; standalone calls
normalize, resolve metadata, and build context without persistence.

Report identity source `yfinance` when metadata was resolved and `symbol_utils` when only canonical
symbol normalization succeeded. Add a warning when yfinance identity metadata is unavailable;
never fail an otherwise valid run solely because identity enrichment failed.

Expose only bounded social arguments:

```python
def fetch_stocktwits_messages(
    self, ticker: str, limit: int = 30, start_date: str | None = None,
    end_date: str | None = None, *, run_id: str | None = None,
    cursor: str | None = None, page_size: int | None = None,
) -> DataToolResult:

def fetch_reddit_posts(
    self, ticker: str, limit_per_sub: int = 5, start_date: str | None = None,
    end_date: str | None = None, *, run_id: str | None = None,
    cursor: str | None = None, page_size: int | None = None,
) -> DataToolResult:
```

Keep the repository's default subreddit list and timing; do not expose arbitrary subreddits,
timeouts, or inter-request delays. Validate limits from 1 through 100.

- [ ] **Step 6: Add configuration leak and no-LLM regression guards**

Patch `tradingagents.llm_clients.factory.create_llm_client` and network entry points to raise if a
pure validation/status/reuse path calls them. Confirm all invalid requests, `get_analysis`, saved
reuse, and continuation complete without network or LLM construction.

- [ ] **Step 7: Run all focused source and plugin tests**

Run:

```bash
pytest tests/test_plugin_data.py tests/test_plugin_store.py \
  tests/test_symbol_utils.py tests/test_ticker_symbol_handling.py \
  tests/test_fred.py tests/test_polymarket.py tests/test_stocktwits_resilience.py \
  tests/test_reddit_fallback.py tests/test_social_lookahead.py -q
ruff check tradingagents/plugin tests/test_plugin_data.py tests/test_plugin_store.py
```

Expected: all pass.

- [ ] **Step 8: Commit the named tools**

```bash
git add tradingagents/plugin/data.py tests/test_plugin_data.py
git commit -m "feat(plugin): expose typed financial evidence tools"
```

---

### Task 6: Register the MCP surface and update capability/protocol contracts

**Files:**
- Modify: `tradingagents/plugin/server.py`
- Modify: `tests/test_plugin_capabilities.py`
- Modify: `tests/test_plugin_runtime.py`
- Modify: `scripts/smoke_plugin_protocol.py`

**Interfaces:**
- Consumes: `PluginStore`, `PluginTools`
- Changes: `create_server(state_root: str | Path) -> FastMCP`
- Registers: `get_capabilities`, `start_analysis`, `get_analysis`, and fifteen data/identity methods

- [ ] **Step 1: Write failing capability and registration tests**

Update capability expectations:

```python
expected_available = {
    "get_capabilities", "start_analysis", "get_analysis",
    "get_stock_data", "get_indicators", "get_verified_market_snapshot",
    "get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement",
    "get_news", "get_global_news", "get_insider_transactions",
    "get_macro_indicators", "get_prediction_markets",
    "resolve_instrument_identity", "fetch_stocktwits_messages", "fetch_reddit_posts",
}
assert set(result.available_tools) == expected_available
assert result.planned_data_tools == ["get_decision_history"]
assert result.analysis_settings.available is True
assert result.schema_version == 2
```

Change the fake FastMCP registration test to call `create_server(tmp_path)` and assert the exact
method-name set. Keep all role `workflow_available` flags false because a complete workflow is not
available yet.

- [ ] **Step 2: Update protocol discovery tests before production registration**

In `tests/test_plugin_runtime.py`, parse the `tools/list` result and assert the exact tool-name set,
not only JSON-RPC framing. Update `scripts/smoke_plugin_protocol.py` to assert the same set and keep
calling `get_capabilities`. Do not call live data in the installed smoke.

- [ ] **Step 3: Run discovery tests and verify expectation failures**

Run:

```bash
pytest tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q
```

Expected: capability and registration mismatches.

- [ ] **Step 4: Wire state root into server construction**

Change `main()` to retain `root = prepare_state_root(args.state_dir)` and call
`create_server(root)`. Implement:

```python
def create_server(state_root: str | Path) -> FastMCP:
    from mcp.server.fastmcp import FastMCP

    from tradingagents.plugin.data import PluginTools
    from tradingagents.plugin.store import PluginStore

    server = FastMCP("TradingAgents", log_level="WARNING")
    tools = PluginTools(PluginStore(state_root))
    server.tool()(get_capabilities)
    for operation in tools.public_operations():
        server.tool()(operation)
    return server
```

`PluginTools.public_operations()` returns an explicit tuple in this order: lifecycle methods,
US-005 tools, then US-006 tools. It contains no reflection-based method discovery.

- [ ] **Step 5: Update capability values and lazy-import guarantees**

Set public schema version `2`, move the seventeen new operations to `available_tools`, leave only
`get_decision_history` in `planned_data_tools`, and set `analysis_settings.available=True`.
`get_capabilities()` must remain pure and must not construct the store, load global config, touch
SQLite, access network, or import an LLM client.

- [ ] **Step 6: Run focused protocol checks**

Run:

```bash
pytest tests/test_plugin_capabilities.py tests/test_plugin_runtime.py \
  tests/test_plugin_data.py tests/test_plugin_store.py -q
python scripts/smoke_plugin_protocol.py
ruff check tradingagents/plugin tests/test_plugin_capabilities.py \
  tests/test_plugin_runtime.py scripts/smoke_plugin_protocol.py
```

Expected: all pass; every stdout line remains valid JSON-RPC.

- [ ] **Step 7: Commit the MCP contract**

```bash
git add tradingagents/plugin/server.py tests/test_plugin_capabilities.py \
  tests/test_plugin_runtime.py scripts/smoke_plugin_protocol.py
git commit -m "feat(plugin): register run and evidence operations"
```

---

### Task 7: Documentation, clean-install proof, and final verification

**Files:**
- Modify: `docs/plugin-runtime.md`
- Modify: `docs/user-testing-us-005-008.md`
- Modify only if required by a real failure: `.github/workflows/ci.yml`

**Interfaces:**
- Verifies the complete US-005–US-008 milestone; produces no new runtime abstraction

- [ ] **Step 1: Update runtime documentation**

Replace the statement that `get_capabilities` is the only tool. List lifecycle/data availability,
state database location, request UUID idempotency, first-stage-only limitation, terminal versus
retryable evidence behavior, and the fact that no LLM API credentials are needed. Keep US-018
manifest/activation explicitly out of scope.

- [ ] **Step 2: Activate the user-testing guide**

In `docs/user-testing-us-005-008.md`, remove the warning that the stories are unimplemented. Check
every example against the final tool signatures and response field names. Retain the direct
`codex mcp add` path because packaged activation remains US-018.

- [ ] **Step 3: Run the complete focused acceptance set**

```bash
pytest tests/test_dataflows_config.py tests/test_vendor_routing.py \
  tests/test_vendor_errors.py tests/test_no_data_handling.py \
  tests/test_plugin_store.py tests/test_plugin_data.py \
  tests/test_plugin_capabilities.py tests/test_plugin_runtime.py \
  tests/test_symbol_utils.py tests/test_ticker_symbol_handling.py \
  tests/test_fred.py tests/test_polymarket.py tests/test_stocktwits_resilience.py \
  tests/test_reddit_fallback.py tests/test_social_lookahead.py -q
```

Expected: all pass.

- [ ] **Step 4: Prove the installed package outside the checkout**

Create a temporary virtual environment, install non-editably, and run the protocol smoke from a
temporary working directory:

```bash
PLUGIN_TEST_ROOT="$(mktemp -d)"
python -m venv "$PLUGIN_TEST_ROOT/venv"
"$PLUGIN_TEST_ROOT/venv/bin/python" -m pip install ".[plugin]"
"$PLUGIN_TEST_ROOT/venv/bin/python" -m pip check
(cd "$PLUGIN_TEST_ROOT" && "$PLUGIN_TEST_ROOT/venv/bin/python" \
  "$OLDPWD/scripts/smoke_plugin_protocol.py")
```

Expected: install, `pip check`, and smoke all exit zero. Record the local Python version; CI remains
the authority for the full Python 3.10–3.13 matrix.

- [ ] **Step 5: Run the repository gates**

```bash
pytest -q
ruff check .
python -m pip wheel . --no-deps -w /tmp/tradingagents-dist
git status --short
```

Expected: tests and lint pass, wheel builds, and status contains only intended documentation
changes. Do not modify CI unless the existing plugin job fails to include one of the new focused
tests; if it does, add `tests/test_plugin_store.py tests/test_plugin_data.py` to that job's pytest
command and rerun its local equivalent.

- [ ] **Step 6: Commit documentation and any verified CI wiring**

```bash
git add docs/plugin-runtime.md docs/user-testing-us-005-008.md .github/workflows/ci.yml
git commit -m "docs: document plugin evidence testing"
```

If `.github/workflows/ci.yml` did not change, omit it from `git add`.

## Final acceptance checklist

- [ ] All fifteen named data/identity tools are discoverable and typed.
- [ ] Existing API data callers preserve their return and exception behavior.
- [ ] Start requests are validated, frozen, restart-safe, and UUID-idempotent.
- [ ] Only the first selected analyst stage exists; permissions reject sibling-stage tools.
- [ ] Terminal evidence is saved once and reused; retryable errors are never saved.
- [ ] Paging reconstructs complete content and rejects cursor misuse.
- [ ] Two differently configured runs cannot leak configuration, including after failure.
- [ ] `get_analysis` and evidence continuation never access the network.
- [ ] Plugin tests cannot construct an LLM client.
- [ ] Focused tests, full pytest, Ruff, wheel build, and installed protocol smoke pass.
