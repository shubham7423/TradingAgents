# Codex plugin staged workflow and recovery: US-009–US-013

Date: 2026-09-17

Status: Written specification approved in conversation; implementation planning has not started.

PRD: [TradingAgents Codex plugin and MCP integration](../../../tasks/prd-tradingagents-codex-plugin-mcp.md).

Architecture: [Overall plugin architecture](2026-09-11-codex-plugin-architecture.md).

Foundation: [US-005–US-008 evidence and run design](2026-09-15-codex-plugin-evidence-runs-design.md).

## Scope and decisions

Deliver US-009–US-013 as one atomic milestone. Complete every selected analyst stage, the
research debate and plan, the trader proposal, the risk debate, and the portfolio decision. Add
restart-safe reads, listings, submissions, receipts, revision checks, and cancellation around the
whole workflow rather than layering recovery onto it later.

The approved decisions are:

- Use a small explicit transition table. Derive the next stage from frozen run configuration and
  accepted outputs; do not persist a precomputed stage queue.
- Accept native submission payloads: strings for narrative roles and JSON objects for structured
  roles.
- Persist both canonical submitted data and rendered Markdown for structured outputs. Canonical
  data is authoritative; rendered Markdown preserves existing prompt and reporting behavior.
- Build the complete role prompt on the server through existing shared prompt builders. Return
  evidence requirements, output schema, and saved-section references as separate metadata without
  duplicating raw context.
- Upgrade existing plugin databases through one explicit additive v1-to-v2 migration. Do not add
  a migration framework.
- Limit each submission to 32,000 characters after canonical serialization.
- After the portfolio decision, set the run status to `ready_to_finalize` and its non-submittable
  stage sentinel to `finalize`. Report export remains US-014.

## Milestone boundary

This milestone provides:

- Complete sequential stage orchestration for US-009–US-012.
- Evidence-gated analyst submissions.
- Native validation of `SentimentReport`, `ResearchPlan`, `TraderProposal`, and
  `PortfolioDecision` objects.
- Immutable accepted outputs and idempotent submission receipts.
- Read, list, resume, and cancel operations with optimistic revisions.
- Database restart, migration, and two-writer safety.

It does not provide report export, decision-history writes, reflection jobs, workflow skill
packaging, a browser UI, background execution, independent role sessions, or LangGraph checkpoint
interop. Those remain in later stories. Do not add a generic workflow engine, repository layer,
event bus, migration framework, or stored stage-plan table.

## Components and boundaries

### Workflow service

Add `tradingagents/plugin/workflow.py`. It owns:

- Stage identifiers, role metadata, and the transition function.
- Reconstruction of the existing `AgentState` shape from frozen run data and accepted rendered
  outputs.
- Calls to the existing shared prompt builders and accepted-output update helpers.
- Required-evidence descriptions and validation.
- Submission normalization, size checks, structured validation, and rendering.
- The `get_analysis`, `submit_stage`, `list_analyses`, and `cancel_analysis` behavior exposed by
  the MCP adapter.

This service performs no model call, data fetch, report export, or generic graph execution. It
does not instantiate `TradingAgentsGraph`.

Expose the sentiment analyst's existing seven-day window calculation as a small public helper and
reuse it in both the API runner and workflow evidence check. No other role module needs a new
prompt path.

### Plugin store

Keep SQL and transactions in `tradingagents/plugin/store.py`. Extend it only with concrete
operations needed by this milestone:

- Consistent run/output/evidence reads.
- Immutable stage-output insertion and retry lookup.
- Transactional stage advancement.
- Filtered run listing with keyset pagination.
- Revision-checked cancellation.
- The explicit database v1-to-v2 migration.

The store does not know role prompts, Pydantic output schemas, evidence requirements, or the
financial meaning of a stage.

### MCP adapter

Extend the existing plugin adapter and `tradingagents/plugin/server.py` to register the new
operations and response models. Preserve the existing `start_analysis` and data-tool contracts.
`get_capabilities` marks the complete analysis workflow as available and reports the new workflow
operations.

## Stage sequence

The transition function follows this exact order:

```text
analyst/<selected key in frozen order>
research/bull/1
research/bear/1
...
research/bull/N
research/bear/N
research/manager
trader
risk/aggressive/1
risk/conservative/1
risk/neutral/1
...
risk/aggressive/R
risk/conservative/R
risk/neutral/R
portfolio
finalize
```

`N` is the frozen research-round count and `R` is the frozen risk-round count. The last selected
analyst advances to `research/bull/1`. Every bull stage advances to the bear stage in the same
round. A bear stage advances to the next bull round or to `research/manager`. The equivalent
three-role rule applies to risk rounds. No caller-provided counter or history can alter the
sequence.

`finalize` is a status sentinel, not a role and not a legal `submit_stage` target. Reaching it
sets `status=ready_to_finalize`; US-014 will add its operation.

## Reconstructing role state

Build the shared role state from persisted facts:

1. Initialize instrument, date, language, frozen lessons, empty reports, and empty debate states
   from the run.
2. Read accepted outputs in workflow order.
3. Use the persisted rendered Markdown as each accepted role's text.
4. Fold that text through the existing shared output-update helper for the role.

This preserves existing role prefixes, debate history, counts, and downstream field names without
storing a second mutable state snapshot. The canonical structured object remains available for
typed reads and auditing. The run's supported `state_schema` and `prompt_schema` must be checked
before reconstruction. Unknown versions return a compatibility error rather than attempting a
best-effort replay.

## Public MCP contract

All workflow responses preserve `run_id`, `status`, `revision`, `current_stage`, and `warnings`.
Pydantic request models forbid unknown fields.

### `get_analysis`

Inputs:

- Required `run_id`.
- Optional `section`.
- Optional `evidence_id`.
- Optional opaque `cursor` and `page_size`, valid only with `evidence_id`.

`section` and `evidence_id` are mutually exclusive.

The default response returns:

- Active role and stage.
- Complete server-built prompt from the existing shared builder.
- Required-evidence checklist with satisfied or missing status.
- The structured role's JSON Schema, or null for narrative roles and the `finalize` sentinel.
- Saved-section names and receipt metadata.
- Compact evidence metadata.
- Frozen configuration, instrument identity, and lessons already returned by the foundation
  contract.

The default response does not duplicate all raw context outside the prompt. A `section` read
returns one accepted output as rendered Markdown with its receipt metadata. An `evidence_id` read
returns one saved page with the existing maximum of 32,000 characters and an explicit continuation
cursor. Every mode is read-only and uses persisted content; none invokes a data source.

Only an active run returns an active role, prompt, evidence checklist, and output schema. A
cancelled run preserves `current_stage` to show where it stopped but returns no active role or
submittable prompt. A ready-to-finalize run returns the `finalize` sentinel and no role prompt.
Completed-run behavior remains reserved for US-014.

### `submit_stage`

Inputs:

- `run_id`.
- `stage_id`.
- `expected_revision`.
- `output`, typed as string or JSON object.

Narrative analyst, bull, bear, and risk roles require a nonempty string. `SentimentReport`,
`ResearchPlan`, `TraderProposal`, and `PortfolioDecision` require an object validated by their
existing Pydantic models. Structured output is rendered once through the existing renderer.
Whitespace-only narrative output and canonical serialized submissions over 32,000 characters are
rejected.

A successful response includes an immutable receipt ID, accepted stage and revision, canonical
output, rendered Markdown, and the next run state. The canonical representation uses stable JSON
serialization, so object key order does not change retry identity.

An identical retry of an already accepted stage returns its original receipt even when its
`expected_revision` is now stale because the run advanced. A different payload for an accepted
stage returns `STAGE_ALREADY_ACCEPTED` and never overwrites it.

### `list_analyses`

Inputs:

- Optional normalized ticker filter.
- Optional status filter.
- `limit`, default 20 and maximum 100.
- Optional opaque continuation cursor.

Return compact summaries ordered by `(created_at, run_id)` newest first. Use keyset pagination so
new runs do not cause duplicates within an existing traversal. Include cancelled and
ready-to-finalize runs when requested.

### `cancel_analysis`

Inputs are `run_id` and `expected_revision`. An active or ready-to-finalize run with the expected
revision is atomically changed to `cancelled`, its revision is incremented, and all evidence and
accepted outputs remain intact. Repeating cancellation for an already cancelled run is idempotent.
A completed run cannot be cancelled. Cancelled runs remain readable and listable but reject new
evidence and submissions. Restarting requires a new `start_analysis` request and run.

## Evidence requirements

`get_analysis` exposes missing requirements early, but `submit_stage` performs the authoritative
check inside its advancement transaction.

`analyst/market` requires a terminal `get_verified_market_snapshot` record for the frozen canonical
ticker and analysis date.

`analyst/social` requires terminal attempts for all of:

- `get_news`.
- `fetch_stocktwits_messages`.
- `fetch_reddit_posts`.

Each must match the frozen ticker and the exact inclusive window from seven calendar days before
the analysis date through the analysis date. Rename the sentiment analyst's existing private
`_seven_days_back` helper to a public `sentiment_window_start` helper and use it in both paths; do
not reproduce the date arithmetic.

Persisted `success`, `no_data`, and `unavailable` records satisfy an attempt. Retryable transport,
authentication, rate-limit, or malformed-response errors remain unpersisted and therefore cannot
satisfy the gate. Other analyst stages have no additional mandatory source requirement. Their
prompts still include all recorded evidence relevant to that role.

## Submission and transaction flow

For a stage submission:

1. Validate the output kind, canonicalize it, enforce the size limit, and perform Pydantic or
   nonempty-text validation without writing.
2. Begin an immediate SQLite transaction.
3. Load the run and check database, state, and prompt compatibility.
4. Look up an accepted output for the submitted stage. Return the existing receipt on an identical
   hash or reject a conflicting hash.
5. Require an active run, the expected revision, and the exact current stage.
6. Recheck required evidence inside the transaction.
7. Insert the immutable stage output and receipt.
8. Derive the next stage and update the run status, stage, revision, and timestamp.
9. Commit the output and advancement together.

Pydantic validation outside the write transaction is safe because the current stage and revision
are rechecked before acceptance. No network call or prompt construction occurs while holding the
write transaction.

## Persistence and migration

Increment the plugin database schema version from 1 to 2. Fresh databases create the complete v2
schema. Existing v1 databases run one explicit migration inside a transaction:

1. Add `stage_outputs`.
2. Add a denormalized nullable run ticker column and backfill every existing row from its frozen
   normalized input or instrument data using Python JSON parsing.
3. Add indexes needed for `(run_id, stage_id)` receipt lookup and filtered newest-first listings.
4. Update the metadata version only after all steps succeed.

The application always writes the ticker for new v2 runs and treats a missing ticker after
migration as incompatible state. Do not rebuild the `runs` table merely to add a database-level
`NOT NULL` constraint.

`stage_outputs` contains:

- Receipt ID primary key.
- Run ID and stage ID with a unique pair.
- Role key and output kind.
- Canonical output JSON.
- Rendered Markdown.
- Canonical output hash.
- Accepted revision and timestamp.

Do not add a separate receipt table: an immutable stage-output row is the receipt.

Unknown schema versions fail startup with `INCOMPATIBLE_STATE`. Unsupported per-run state or
prompt versions fail reads and writes for that run without deletion, reset, or implicit upgrade.

## Concurrency and recovery

SQLite `BEGIN IMMEDIATE` serializes submit and cancel writers across MCP server processes. If two
writers use the same revision, one commits first and the other observes either the immutable
accepted output or a stale revision. No check-then-write race may advance a stage twice.

Read a run, its accepted outputs, and its evidence within one read transaction when building
`get_analysis`, so the prompt and reported revision describe one snapshot.

Evidence fetching remains outside a write transaction. The existing save-time status and stage
recheck rejects evidence that returns after the run advances or is cancelled. Server restart needs
no replay log: the frozen run, evidence, stage outputs, and current stage are sufficient to rebuild
the same pending role state.

## Error contract

Return stable machine-readable codes with human-readable messages:

- `VALIDATION_ERROR`: wrong output kind, empty text, oversized content, or structured field errors.
- `MISSING_EVIDENCE`: required terminal attempt absent; include expected tool and canonical
  arguments.
- `STALE_REVISION`: expected revision does not match an unaccepted current stage.
- `WRONG_STAGE`: submitted stage is not the current pending stage.
- `STAGE_ALREADY_ACCEPTED`: an immutable stage has a different accepted payload.
- `RUN_CANCELLED`: a cancelled run received a mutating request.
- `RUN_NOT_ACTIVE`: the run is completed or ready to finalize for an unsupported mutation.
- `INCOMPATIBLE_STATE`: database, state, or prompt schema is unsupported.
- Existing `RUN_NOT_FOUND` semantics remain unchanged.

Structured validation errors preserve Pydantic field locations and messages. Every validation,
conflict, compatibility, and stale-write failure leaves the run unchanged.

## Verification

Add the smallest focused checks that establish the workflow invariants:

- Selected analysts run once in frozen order; skipped analysts never appear.
- Market and sentiment evidence gates accept terminal unavailable/no-data attempts and reject
  absent or mismatched evidence.
- Invalid `SentimentReport`, `ResearchPlan`, `TraderProposal`, and `PortfolioDecision` objects
  return field-level errors without advancement.
- One and multiple research rounds preserve bull-then-bear order.
- One and multiple risk rounds preserve aggressive-then-conservative-then-neutral order.
- An N=2/R=2 fixture-backed run records exactly four research and six risk contributions before
  reaching `ready_to_finalize`.
- Trader prompts include the research plan and selected market report; risk prompts include the
  proposal, reports, and relevant history; portfolio prompts include plans, risk history,
  instrument context, and frozen lessons.
- Opening debate prompts use the existing absent-opponent marker.
- Identical retries return one receipt; conflicting retries, wrong stages, stale revisions, and
  two-writer races never overwrite or double-advance.
- Restart at representative analyst, research, risk, and ready-to-finalize stages preserves the
  same revision, prompt inputs, outputs, evidence, and frozen configuration.
- Cancellation preserves reads and rejects later evidence/submission writes.
- v1 migration preserves existing runs/evidence; unknown database and per-run versions fail
  closed.
- Listing filters, keyset pagination, saved-section reads, and evidence paging are deterministic.
- Protocol discovery and calls expose the expanded typed contract without any model-client
  construction.

Run focused plugin/shared-role tests during development, then the full `pytest -q` suite and
`ruff check .`. No new test framework, dependency, or static type-checking gate is required.

## Suggested implementation slices

These are boundaries for the later implementation plan, not permission to merge partially working
workflow behavior:

1. Add and test the v2 migration and immutable stage-output store operations.
2. Add the explicit transition function and deterministic state reconstruction.
3. Add evidence requirements, prompt descriptions, native validation, rendering, and transactional
   submission.
4. Add read modes, listings, cancellation, and MCP registration.
5. Add full-sequence, restart, race, migration, protocol, regression, and lint verification.

The milestone is complete only when all five stories and their recovery invariants pass together.
