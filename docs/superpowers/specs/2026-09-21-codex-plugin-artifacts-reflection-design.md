# Codex Plugin Artifacts and Reflection Design

Date: 2026-09-21

Scope: US-014 through US-016 of `tasks/prd-tradingagents-codex-plugin-mcp.md`.

## Intent and success criteria

Extend the durable Codex plugin workflow from `ready_to_finalize` through report export,
decision history, measured outcomes, and Codex-authored reflection. Python owns deterministic
calculation, validation, persistence, retry behavior, and report generation. Codex remains the
only model that authors plugin reflections; the plugin runtime must not construct an LLM client.

The milestone succeeds when:

- finalization writes the existing report tree to a stable run-owned directory and records one
  canonical decision before marking the run completed;
- export retries cannot duplicate decisions or corrupt concurrent existing-runner writes;
- history reads are bounded, path-free, and point-in-time correct;
- reflection preparation creates durable jobs only after a full five-session outcome is known by
  the requested cutoff;
- reflection jobs use the existing read, submit, and finalize operations and update their intended
  decisions exactly once; and
- later historical analyses receive only lessons resolved by their analysis date.

## Confirmed decisions

- The shared Markdown memory log remains canonical. SQLite tracks plugin workflow and export
  receipts but does not replace decision history.
- Outcomes use exactly five trading sessions. Free-text portfolio horizons do not alter this
  window.
- History `as_of_date` is a true point-in-time cutoff: later decisions are excluded, while an
  earlier decision resolved after the cutoff appears pending without its future outcome or
  reflection.
- Reflection jobs use a dedicated table rather than making the existing analysis tables
  polymorphic.
- This milestone adds runtime support for recording an explicit learning-step omission. Automatic
  prepare-and-complete orchestration remains part of the US-017 `trading-analysis` skill because
  that skill does not yet exist in the repository.

## Non-goals

- Do not create a generic job framework, repository layer, queue, worker, or migration framework.
- Do not infer a holding period from `PortfolioDecision.time_horizon`.
- Do not add alternate reflection endpoints; reflection jobs reuse `get_analysis`, `submit_stage`,
  and `finalize_analysis`.
- Do not add a locking dependency or change the canonical history format wholesale.
- Do not create the US-017 skill in this milestone.

## Persistence model

Upgrade the plugin database through one explicit additive v2-to-v3 migration. Unknown schema
versions continue to fail before mutation.

### Analysis export receipts

Add an analysis export receipt keyed by `run_id`. It stores:

- stable decision identity;
- report directory and consolidated-report path;
- emitted section paths;
- portfolio rating; and
- completion timestamp.

The receipt is the replay result for a completed analysis. It prevents a completed retry from
rewriting external artifacts and gives callers durable paths after restart.

### Reflection jobs

Add a `reflection_jobs` table with:

- job ID and stable decision identity;
- ticker, decision date, rating, and immutable decision text;
- raw return, benchmark-relative return, benchmark identity, holding sessions, and resolution
  date;
- status, revision, current stage, accepted reflection, and its content hash;
- creation, update, and completion timestamps; and
- a uniqueness constraint over the decision identity.

Statuses mirror the needed subset of analysis behavior: `active`, `ready_to_finalize`, and
`completed`. The sole reasoning stage is `reflection`. Identical preparation, submission, and
finalization retries return the existing job or receipt. Conflicting submissions cannot overwrite
accepted text.

Network price retrieval happens outside SQLite transactions. Before inserting a job, preparation
rechecks that the target decision is still pending. SQLite write transactions remain short and use
the store's existing immediate-write and busy-timeout conventions.

## Canonical memory format and concurrency

New plugin decisions use the analysis `run_id` as their stable decision identity and include it as
an invisible entry marker:

```markdown
[2026-09-21 | AAPL | Buy | pending]

<!-- decision_id: 93f... -->

DECISION:
...
```

The marker does not alter the existing leading tag or rendered decision text. The parser accepts
entries without a marker. It derives a legacy identity from the immutable decision date, ticker,
rating, and decision text so supported legacy pending entries can be targeted without first
rewriting the log. New resolved tags retain the current leading fields and append explicit
benchmark and resolution metadata. Missing legacy metadata is represented as unknown, never
invented.

A legacy entry is updateable only when that derived identity selects exactly one block. If a
hand-edited log contains duplicate indistinguishable legacy blocks, history returns both with a
warning but reflection preparation refuses to target either; it never guesses which block to
rewrite.

All memory mutations use a sibling lock file for cross-process serialization. Under that lock,
the writer rereads the latest log, applies its identity-based change, writes a temporary file, and
atomically replaces the log. This applies to append, outcome/reflection update, and rotation, so
the plugin and existing API runner cannot erase one another's changes. The implementation uses
the Python standard library's platform locking primitives rather than a new dependency.

When a stable identity is supplied, decision insertion is idempotent by identity. Existing-runner
callers that do not supply one retain their current same-ticker/same-date duplicate guard. Rotation
continues to preserve pending entries and cap only resolved entries.

## Analysis finalization

`finalize_analysis(run_id)` accepts an analysis in `ready_to_finalize` and performs these ordered
steps:

1. Reconstruct the final domain state from accepted stage outputs using the existing workflow
   replay path.
2. Call the existing report-tree writer for the fixed directory
   `<results_dir>/plugin/<run_id>/`. Supply a stable generated timestamp so retries do not change
   report content. Partial files are overwritten on retry.
3. Determine the rating from the accepted structured portfolio decision. Do not invoke a model or
   silently coerce an unparseable value.
4. Upsert the final decision into canonical memory using `decision_id=run_id` under the shared
   memory lock.
5. In one SQLite transaction, insert the export receipt and move the run to `completed`.

The completed response returns the report directory, consolidated report, section paths, rating,
and decision identity. Re-finalizing a completed run returns its stored receipt without rewriting
artifacts.

Report and memory files cannot share the SQLite transaction. A report failure or memory failure
therefore leaves the run `ready_to_finalize`. A crash after memory export but before SQLite
completion is safe: retry rewrites the run-owned reports, finds the same memory identity, and then
records completion without duplicating the decision.

## Read-only decision history

Add a named, typed `get_decision_history` operation. It accepts only optional `ticker`, optional
`as_of_date`, a bounded `limit`, and an opaque cursor. It always reads the server-configured memory
log and never accepts a path.

Results include the stable or derived decision identity, decision date, ticker, rating, pending
state, decision text, available outcome fields, benchmark identity, resolution date, and
reflection. Pagination follows canonical log order with an opaque continuation cursor.

For a point-in-time read:

- exclude decisions dated after `as_of_date`;
- include a decision dated on or before the cutoff;
- expose its outcome and reflection only when `resolution_date <= as_of_date`; and
- otherwise project that entry as pending and omit future outcome/reflection fields.

Malformed legacy blocks are skipped and reported through response warnings. Reads never repair or
rewrite the log.

## Shared outcome calculation

Move benchmark selection and deterministic return calculation out of
`TradingAgentsGraph` into model-free functions in `tradingagents/graph/reflection.py`. Keep thin
graph methods or direct calls as needed to preserve existing API-runner behavior and tests.

The shared calculator:

- resolves an explicit configured benchmark first, then the existing exchange-suffix map, then
  the US default;
- normalizes the traded symbol through the existing symbol utility;
- uses entry close and the close exactly five trading sessions later for both instrument and
  benchmark;
- requires both series to contain the complete window;
- stores raw return, benchmark-relative return, benchmark identity, holding sessions, and the
  instrument's fifth-session date as the resolution date; and
- accepts an `as_of_date` bound so preparation neither resolves nor exposes an outcome known after
  the requested historical cutoff.

Unavailable, incomplete, or invalid price data returns an unavailable outcome and leaves memory
unchanged. The existing API runner continues logging and retrying such entries on a later
same-ticker run.

## Reflection preparation and lifecycle

`prepare_reflections(ticker, as_of_date)` validates and canonicalizes its inputs, then reads
same-ticker pending decisions from canonical memory. For each candidate it calls the shared
five-session outcome calculator. It creates a job only when the complete outcome is available and
its resolution date is no later than `as_of_date`.

Repeated preparation returns the existing job for the same decision/outcome. Too-recent decisions
and unavailable outcomes remain pending and produce no job. Preparation performs no model call and
does not mark a decision resolved.

The existing workflow operations dispatch by identifier:

- `get_analysis(job_id)` returns `active`, stage `reflection`, the existing concise reflection
  prompt, immutable decision text, and measured outcome metadata.
- `submit_stage(job_id, "reflection", expected_revision, output)` accepts a nonempty string within
  the existing submission bound, stores it once, and advances the job to `ready_to_finalize`.
  The prompt requests two to four plain-prose sentences; Python does not add a brittle sentence
  counter.
- `finalize_analysis(job_id)` finds the intended pending decision by stable identity, writes the
  measured outcome and Codex-authored reflection under the memory lock, and then marks the job
  completed in SQLite.

If another process resolves the decision after preparation, finalization preserves the first
resolution rather than overwriting it. The job becomes completed with `changed=false` and an
explicit already-resolved warning, avoiding an indefinitely stuck retry. An identical completed
retry returns that receipt.

`start_analysis` adds `skip_reflections: bool = False`. An explicit true value is included in the
request fingerprint and frozen normalized inputs as `learning_omitted`, making that choice visible
after restart. A normal start still loads lessons through `TradingMemoryLog.get_past_context` with
the analysis date, which already excludes lessons whose resolution date is later than a historical
run. US-017 will instruct the future skill to prepare and complete eligible jobs before a normal
same-ticker start and to set this flag only when the user explicitly skips learning.

## Public responses and dispatch

Keep existing analysis response fields stable. Add a work-kind discriminator and the reflection
fields needed when an identifier belongs to a reflection job. Analysis finalization returns an
artifact receipt; reflection finalization returns its decision identity, outcome metadata, and
completion state. Public error messages distinguish missing analysis IDs, missing reflection IDs,
wrong work kind, stale revision, conflicting retry, unavailable outcome, and already-resolved
decisions.

`get_capabilities` and MCP registration advertise `finalize_analysis`, `prepare_reflections`, and
`get_decision_history` as available. No operation accepts an output directory, memory path,
function name, or executable content.

## Failure behavior

- Invalid state, wrong stage, or stale revision changes neither SQLite nor external files.
- Report and memory export errors retain `ready_to_finalize` and surface a retryable error with the
  same identifier.
- An interrupted memory replacement leaves either the old complete file or the new complete file.
- Reflection preparation records no partial job for an unavailable outcome.
- A reflection update occurs at most once for one decision identity. Conflicting already-resolved
  content is preserved and reported rather than replaced.
- Unsupported database, state, or prompt schemas remain compatibility errors; there is no reset or
  best-effort replay.

## Implementation boundary

Modify only the existing concrete boundaries:

- `tradingagents/agents/utils/memory.py` for identities, benchmark metadata, history projections,
  and locked atomic mutation;
- `tradingagents/graph/reflection.py` and `tradingagents/graph/trading_graph.py` for shared outcome
  calculation without changing API-runner behavior;
- `tradingagents/reporting.py` for a stable optional generation timestamp while retaining its
  existing default;
- `tradingagents/plugin/store.py` for schema v3, export receipts, and reflection jobs;
- `tradingagents/plugin/workflow.py` for analysis finalization, reflection stages, and identifier
  dispatch;
- `tradingagents/plugin/data.py` and `tradingagents/plugin/server.py` for typed public models,
  operations, registration, and capability reporting; and
- runtime documentation and a US-014-through-US-016 user-testing checklist.

Do not add a separate outcome module, service hierarchy, or plugin skill for this milestone.

## Verification

Focused automated checks cover:

- existing report-tree content, stable paths and rating, completed replay, partial report retry,
  and a fault after memory export but before completion;
- stable decision upserts, two-process plugin/API-runner writes, locked rotation, and legacy entry
  parsing;
- bounded history pagination and true point-in-time projection;
- fixed five-session outcomes, configured and suffix benchmarks, historical cutoffs, incomplete
  windows, unavailable data, and duplicate preparation;
- reflection read/submit/finalize behavior, stale and identical retries, exact legacy targeting,
  and independently resolved targets;
- historical lesson filtering and persisted `learning_omitted` state;
- plugin tests that fail if an LLM-client factory is called; and
- protocol registration and capability contracts for all new operations.

Run the focused modules during development, then `pytest -q` and `ruff check .`. Document the
commands and results in `docs/user-testing-us-014-016.md`.
