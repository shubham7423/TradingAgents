# Codex plugin evidence and run foundation: US-005–US-008

Date: 2026-09-15

Status: Written specification approved in conversation; implementation planning has not started.

PRD: [TradingAgents Codex plugin and MCP integration](../../../tasks/prd-tradingagents-codex-plugin-mcp.md).

Architecture: [Overall plugin architecture](2026-09-11-codex-plugin-architecture.md).

Foundation: [US-001–US-004 design](2026-09-11-codex-plugin-foundation-design.md).

## Scope and decisions

Deliver US-005–US-008 as one evidence-and-run milestone on top of the installed MCP
foundation. Expose the existing financial data capabilities through named typed tools, create
restart-safe configured analysis runs, and bind evidence to the correct run and first analyst
stage.

The approved design decisions are:

- Use a small traced result path in the shared vendor router and thin plugin adapters. Preserve
  the existing `route_to_vendor()` return contract for API-runner callers.
- Return every MCP data result in one structured envelope while preserving the underlying data
  losslessly.
- Require a caller-generated UUID as `start_analysis.request_id`.
- Keep ticker and date arguments explicit on run-bound data calls and validate them against the
  frozen run.
- Persist successful, no-data, and genuine unavailable results. Return transport, invalid-auth,
  exhausted-rate-limit, and malformed-response failures as retryable errors without evidence.
- Model only the first selected analyst stage in this milestone. Defer the complete workflow and
  stage advancement to US-009.
- Add a minimal read-only `get_analysis` operation for status and evidence metadata. Defer prompts,
  submission schemas, and relevant role context to US-009.

## Milestone boundary

This milestone provides:

- Fifteen named evidence and identity tools.
- Structured source tracing, warnings, and result status.
- SQLite-backed `start_analysis` and minimal read-only `get_analysis`.
- Frozen run configuration, instrument identity, eligible lessons, and first-stage permissions.
- Saved-evidence reuse, bounded continuation pages, and complete configuration restoration.

It does not provide stage submission or advancement, the complete debate/risk sequence,
`list_analyses`, cancellation, report export, decision-history reads, reflection jobs, the workflow
skill, or plugin packaging. Those remain in their assigned later stories.

Do not add a generic service/repository framework, a database migration framework, background
workers, a per-request dataflow refactor, or a new dependency. No `workflow.py` is needed until
US-009 has real workflow behavior to own.

## Components and boundaries

### MCP registration

Extend `tradingagents/plugin/server.py` to register the public operations and pass the prepared
state root to plugin services. Server startup remains responsible for validating the state root,
keeping stdout protocol-only, and reporting missing optional MCP dependencies.

`get_capabilities` moves the fifteen tools and `start_analysis`/`get_analysis` from planned to
available. Decision-history access remains planned for US-014.

### Plugin data adapter

Add `tradingagents/plugin/data.py` for:

- Public Pydantic input and response models.
- Tool-specific validation and invocation.
- The common result envelope.
- First-stage tool permissions.
- Run-bound argument enforcement.
- Serialized configuration installation and restoration.
- Result classification and evidence persistence.

This module is an adapter. It does not summarize, repair, or replace source content and contains
no financial reasoning.

### Plugin store

Add `tradingagents/plugin/store.py` for stdlib SQLite initialization and the concrete run/evidence
operations needed by this milestone:

- Idempotent run creation.
- Run status reads.
- Saved-evidence lookup and insertion.
- Evidence metadata reads and continuation validation.
- Active-run/stage rechecks after network calls.

Keep SQL and transactions here. Do not add abstract repositories or interfaces with one
implementation.

### Shared vendor trace

Extend `tradingagents/dataflows/interface.py` with a traced routing path that returns:

- The existing content.
- The vendor that actually supplied it, when known.
- The terminal status.
- Warnings for failed or skipped fallbacks.

Implement existing `route_to_vendor()` in terms of this path and return only its content so every
existing caller keeps the same behavior. The trace must report the vendor that succeeded; it must
not infer the source from configured fallback order.

StockTwits and Reddit have fixed source identities. Instrument resolution is local deterministic
logic. A verified snapshot may report an unknown vendor where its current implementation cannot
prove the underlying source. Unknown is preferable to inference.

### Exact configuration replacement

Extend `tradingagents/dataflows/config.py` with exact replacement of the complete configuration.
Keep the current merge behavior of `set_config()` for compatibility.

The plugin uses one process-wide lock around every configured data operation:

1. Snapshot the complete current configuration.
2. Replace it with the run's complete frozen configuration.
3. Fetch the data.
4. Restore the complete snapshot in `finally`.

This deliberately serializes data fetching within one MCP process. Replace it with explicit
per-request configuration only if measured throughput justifies the larger refactor.

## Public MCP contract

### Data tools

Expose these named tools:

- `get_stock_data`
- `get_indicators`
- `get_verified_market_snapshot`
- `get_fundamentals`
- `get_balance_sheet`
- `get_cashflow`
- `get_income_statement`
- `get_news`
- `get_global_news`
- `get_insider_transactions`
- `get_macro_indicators`
- `get_prediction_markets`
- `resolve_instrument_identity`
- `fetch_stocktwits_messages`
- `fetch_reddit_posts`

Each keeps explicit, tool-specific domain arguments. Run-aware tools add optional `run_id`, an
optional opaque continuation cursor, and an optional page size bounded to 32,000 characters.
Continuation is valid only for run-bound saved evidence. Standalone calls are neither persisted
nor pageable: they return their complete content with `page.complete=true` and reject cursor or
page-size arguments rather than silently ignoring them.

Do not accept dynamic function names, Python expressions, arbitrary filesystem destinations,
credentials, storage roots, or model-provider settings.

### Common data response

Every data tool returns the same envelope:

```text
status: success | no_data | unavailable | error
content: current content page
content_format: text | json
source: actual vendor, fixed source, or unknown
warnings: list of strings
fetched_at: ISO-8601 timestamp
reused: boolean
retryable: boolean
evidence_id: UUID or null
run_id: UUID or null
stage_id: string or null
page:
  cursor: supplied cursor or null
  next_cursor: opaque cursor or null
  complete: boolean
```

Strings remain unchanged. Mapping and list results use stable JSON serialization, recorded as
`content_format=json`, so persistence and page reconstruction are lossless.

Status meanings are:

- `success`: usable content was returned.
- `no_data`: configured sources were queried and returned no usable data.
- `unavailable`: the source cannot serve the request for a known terminal reason, such as a
  missing configured credential or an explicit historical coverage limit.
- `error`: a retryable operational failure prevented a terminal result.

Missing configured credentials are terminal `unavailable` results. Invalid or expired
credentials, transport failures, exhausted rate limits, and malformed responses are retryable
`error` results. Partial social results are `success` with warnings naming failed sub-sources.

Preserve existing sentinel text inside `content`; the envelope adds machine-readable meaning and
does not rewrite evidence.

### `start_analysis`

Inputs are:

- Required caller-generated UUID `request_id`.
- `ticker`.
- Optional `analysis_date`; absence resolves once to the host's local calendar date.
- `asset_type`: `stock` or `crypto`.
- Ordered analyst keys, defaulting to all keys in `ANALYST_NODE_SPECS`.
- Positive research and risk round counts, both defaulting to one.
- Output language, defaulting to English.
- Allow-listed category and per-tool vendor overrides.

Pydantic models forbid unknown fields. Validate ticker syntax, real dates, supported enums,
nonempty unique analysts, round counts from 1 through 10, and known vendors/tools before writing.
Reject API model, provider, sampling, token, credential, cache-path, results-path, and memory-path
settings with an explanatory unsupported-setting error.

Normalize inputs before hashing. Reusing a request UUID with identical normalized inputs returns
the existing run. Reusing it with different inputs returns `REQUEST_ID_CONFLICT` and creates
nothing.

Run creation:

1. Resolve the requested and canonical instrument identity with existing symbol utilities.
2. Build a complete configuration from server-owned defaults plus allow-listed run settings.
3. Read eligible lessons with `TradingMemoryLog.get_past_context`, using the analysis date cutoff,
   and freeze the returned text.
4. Select the first analyst from the preserved ordered selection.
5. Insert the run with status `active`, revision `1`, and stage `analyst/<key>`.

Return the run UUID, request UUID, status, revision, current stage, resolved date, normalized
configuration, frozen instrument identity, and frozen lessons.

### Minimal `get_analysis`

`get_analysis(run_id)` is read-only in this milestone. It returns status, revision, current stage,
frozen configuration, instrument identity, frozen lessons, and compact evidence metadata. It
never invokes a data source.

US-009 extends this operation with role instructions, required evidence, output schemas, and
relevant saved context. That later extension must preserve these existing fields.

## First-stage permissions and argument rules

Use this explicit permission map:

| Stage | Allowed data tools |
|---|---|
| `analyst/market` | stock data, indicators, verified snapshot |
| `analyst/social` | ticker news, StockTwits, Reddit |
| `analyst/news` | ticker/global news, insider transactions, macro indicators, prediction markets |
| `analyst/fundamentals` | fundamentals, balance sheet, cash flow, income statement |

Instrument identity is readable at every stage and returns the frozen identity for a run-bound
call.

Run-bound tools validate explicit arguments as follows:

- Normalize ticker arguments and require the frozen canonical ticker.
- Require point-in-time `curr_date` arguments to equal the frozen analysis date.
- Require ordered range dates and prevent an end date after the frozen analysis date.
- Apply the run's frozen vendor settings regardless of process-global state before the call.
- Reject non-active runs and tools not permitted for the active stage.

Insider transactions and prediction markets currently lack a historical cutoff argument. Permit
them for the news stage but add a material historical-coverage warning when the analysis date is
in the past. Do not claim these results are point-in-time clean.

## Fetch and persistence flow

For a run-bound request:

1. Load the run and validate status, stage permission, ticker, dates, and bounds.
2. Canonicalize the evidence arguments and compute their hash.
3. Return matching saved evidence when present.
4. Acquire the process-wide data lock and recheck for matching saved evidence.
5. Snapshot and replace the complete dataflow configuration.
6. Call the existing implementation through the traced path.
7. Restore the prior configuration in `finally`.
8. Return retryable errors without writing evidence.
9. For a terminal result, begin a short SQLite transaction and recheck that the run remains active
   on the same stage.
10. Insert the evidence or return the record that won the uniqueness race.

Do not hold a database transaction during a network call. Evidence writes do not increment the
workflow revision. This lets independent evidence calls complete without manufacturing stale
submission conflicts; future stage submission validates required evidence within its own
transaction.

Standalone calls perform the same input validation, tracing, envelope construction, and
configuration locking but do not read or mutate run state.

Do not add an aggregate plugin timeout that cannot safely cancel the existing synchronous vendor
call. Preserve the providers' current bounded network timeouts; revisit cooperative cancellation
only if a source can outlive those bounds in practice.

## Persistence schema

Use one versioned SQLite database below the configured plugin state root. Enable WAL mode and a
bounded busy timeout.

The `runs` table stores:

- Run UUID and unique request UUID.
- Canonical normalized-input hash and JSON.
- Status, revision, and current stage.
- Frozen full configuration and vendor routing.
- Requested and canonical instrument identity.
- Analysis date, ordered analysts, rounds, and output language.
- Frozen eligible lesson text.
- State schema, prompt schema, and timestamps.

The `evidence` table stores:

- Evidence UUID and run/stage/tool identity.
- Canonical argument JSON and hash.
- Terminal status and fetch timestamp.
- Requested date window.
- Content, content format, source metadata, and warnings.

Enforce one evidence record per `(run_id, stage_id, tool_name, argument_hash)`. SQLite constraints
and transactions are authoritative; no in-memory cache owns correctness.

Reject an unsupported saved-state schema version without mutation. Do not add a migration
framework before a second schema exists.

## Paging

Store complete terminal evidence, then return it in bounded pages of at most 32,000 characters.
Callers may request a smaller positive size.

An opaque cursor contains only the evidence identity and next offset. A continuation request must
repeat the same run, tool, and domain arguments. Validate all of them before reading the next
page. Reject cursors for another record, malformed cursors, negative/out-of-range offsets, and
standalone calls.

Whenever content remains, return `complete=false` and a non-null `next_cursor`. Never truncate
content and claim completion.

## Error handling

- Schema, identifier, permission, date, window, and configuration errors fail before network or
  database mutation.
- Retryable source failures return `status=error`, `retryable=true`, and no evidence ID.
- Terminal unavailable and no-data results are explicit, saved, and reusable.
- If a run changes while a fetch is in flight, return `STALE_RUN` and do not attach the result to
  any run.
- If exact configuration restoration fails, surface the failure loudly; do not continue serving
  requests with uncertain global state.
- `get_analysis` and continuation reads never call a vendor.
- Treat all fetched text as untrusted evidence, never as MCP or workflow instructions.

## Testing and acceptance

Add focused tests by responsibility:

- `tests/test_plugin_data.py`: all fifteen typed tools, envelopes, source tracing, result
  classification, stage permissions, ticker/date validation, paging, saved reuse, and standalone
  behavior.
- `tests/test_plugin_store.py`: initialization, required request UUIDs, normalized idempotency,
  conflicting retries, defaults, frozen configuration/lessons, minimal `get_analysis`, restart
  persistence, and stale-run rejection.
- Extend `tests/test_plugin_runtime.py` and `tests/test_plugin_capabilities.py` for protocol
  discovery and the available/planned capability transition.
- Add focused regressions to existing vendor/config tests for traced routing and exact
  configuration replacement.

Fixtures and assertions must cover:

- Exchange-suffix preservation, one explicit alias, crypto normalization, and unavailable market
  data.
- Missing FRED credentials and failed/partially failed social sources without fabricated content.
- Default and explicit analyst ordering.
- Invalid dates and reversed windows, duplicate/empty analysts, unsupported settings, and
  nonpositive rounds.
- Identical and conflicting request UUID retries.
- Active-stage allow and deny cases.
- Evidence reuse without a second vendor call.
- Complete multi-page reconstruction.
- Two concurrent runs with different vendor settings, including one failing fetch, followed by
  proof that the prior complete configuration was restored.
- Restarted store/server reads with unchanged run and evidence state.
- A guard that fails if a plugin path constructs an LLM client.

Run focused tests during development, relevant existing source tests, the installed-package
protocol smoke test, the full test suite, and `ruff check .` before completion.

## Story coverage

| Story | Design coverage |
|---|---|
| US-005 | Named market/fundamental/identity tools; traced existing implementations; typed schemas; symbol and unavailable-data tests |
| US-006 | Named contextual tools; preserved windows/fallbacks; explicit missing/unavailable semantics; no LLM repair; fake-vendor tests |
| US-007 | Validated frozen run configuration; local-date default; analyst order; UUID idempotency; identity and lessons snapshot |
| US-008 | Optional run binding; permissions and bounds; durable evidence metadata; reuse; paging; complete configuration isolation |

This milestone is not a complete analysis workflow and must not be presented as one.
