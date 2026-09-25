# Codex plugin skill and local installation design

Date: 2026-09-23

Scope: US-017 and US-018 of `tasks/prd-tradingagents-codex-plugin-mcp.md`.

## Intent and success criteria

An existing TradingAgents user can ask Codex for an analysis in ordinary language and receive the
saved decision and report paths without operating each MCP stage. The same installed plugin also
supports resume, standalone data, history, and reflection requests. This first release targets a
personal local installation in Codex desktop and CLI. Codex authors all role outputs in one
conversation; Python owns data retrieval, prompts, stage order, validation, storage, and exports.

The release succeeds when a fresh installed Codex session discovers the skill and all MCP tools,
completes a real analysis with LLM API credentials absent from the Python runtime, saves its
reports and decision, and can resume a separate interrupted run from persisted state. Sequential
roles share conversation context; the skill does not claim independent role isolation or a fixed
subscription allowance.

## Decisions

- Use a thin `trading-analysis` skill over the existing MCP contract. `get_analysis` already
  returns the pending role, instructions, required evidence, output schema, revision, and saved
  sections. Add no orchestration endpoint unless installed acceptance exposes a concrete gap.
- Complete eligible same-ticker reflection jobs before starting a new analysis. An explicit user
  request to skip that step maps to `skip_reflections=true`, so the run records the omission.
- Retry one retryable source failure once. Repair a rejected role submission once using the
  returned field errors and schema. If either still fails, stop and report the pending run ID,
  stage, and error. Never convert a failed fetch into successful or verified data.
- Use the supported Codex compatibility package layout already selected by the repository
  architecture: `.codex-plugin/plugin.json`, `.mcp.json`, and `skills/trading-analysis/SKILL.md`.
  Current OpenAI guidance supports this layout; a portable root manifest belongs to a later
  cross-host milestone.
- Require one complete installed analysis on either Codex desktop or CLI. On the other host,
  verify plugin and tool discovery plus recovery from a persisted run. Record which host ran each
  check, its version, date, and result.

## Skill behavior

The skill recognizes five intents: new full analysis, resume, standalone data retrieval, decision
history, and reflection. It uses only named MCP tools for these operations and never invokes the
existing API runner, creates an LLM client, launches a nested agent, or treats source text as
instructions. It keeps financial role prompts in Python; it does not copy them into the skill.

For a new analysis, the skill resolves missing instrument or date information only when needed to
avoid ambiguity. Otherwise it uses the runtime defaults: current analysis date, stock context,
all analysts, one research round, one risk round, and English output. User-specified supported
settings override those defaults. It calls `prepare_reflections` for the ticker and analysis date,
then reads, submits, and finalizes each eligible reflection job. If the user explicitly skips
learning, it does not prepare jobs and starts the run with `skip_reflections=true`. It creates a
fresh request UUID for `start_analysis` using a local UUID utility and retains the returned run
ID. If an eligible reflection job cannot complete, it stops before starting the new analysis and
reports the job ID. The user may then explicitly request analysis without the learning step.

For any active analysis or reflection job, the skill reads `get_analysis` and follows its current
stage. It fetches each unsatisfied required evidence item with the returned arguments, reads all
pages until `page.complete`, and preserves status, warnings, and source identity in its reasoning.
It uses the stage's returned instructions and schema to author exactly one role output. Narrative
roles submit native text; structured roles submit native JSON objects. It supplies the returned
stage ID and expected revision to `submit_stage`, then reads the next stage. It does not advance
on its own counters. At `ready_to_finalize`, it calls `finalize_analysis` and returns the rating,
decision identity, and report paths from the export receipt.

For resume, a supplied run ID goes directly to `get_analysis`. If the user lacks an ID, the skill
uses `list_analyses` and asks the user to choose only when multiple runs remain plausible. Saved
sections and evidence remain authoritative; a resumed run does not regenerate accepted outputs or
refetch terminal evidence by default. For a completed run, it replays `finalize_analysis` to
obtain the saved export receipt. A cancelled run cannot resume and needs a new request. Standalone
data requests call the named data tools without creating a run. History requests use
`get_decision_history` and its filters and pagination.
Explicit reflection requests use `prepare_reflections`, then the same read, submit, finalize loop.

## Errors and trust boundaries

The skill treats data-tool content, retrieved news, social posts, and report text as evidence, not
commands. It does not follow instructions embedded in them. `success`, `no_data`, and
`unavailable` retain their distinct meanings. It follows all content pages before claiming to
have read a complete result. A retryable source error gets one identical retry. A
field-validation rejection gets one corrected submission using the returned errors and schema.
If a submission or finalization response is lost, the skill reads the run before retrying the
idempotent operation. After a persistent error, stale revision, or conflicting retry, it reports
the actual pending state rather than guessing that a submission succeeded.

## Packaging and setup

Add the compatibility plugin under `plugins/tradingagents/` with a manifest pointing to its skill
directory and `.mcp.json`, plus a short README. `.mcp.json` declares the local stdio
`tradingagents-mcp` executable. Installation instructions create a dedicated environment using a
supported pinned Python version, install the package with its pinned `mcp==1.26.0` plugin extra,
and make the executable visible to the Codex host. The guide shows how to replace the command
with that environment's absolute executable path when the host does not inherit its `PATH`.
Python dependencies are provisioned separately from plugin activation.

The guide covers optional data credentials, the writable state directory and results root,
plugin activation in a personal local marketplace, and verification from a new Codex session.
Credentials are environment variables, never values in plugin files. It states that data sources
may be unavailable, that Codex subscription access is host controlled, and that the local plugin
does not grant unlimited model usage. No public marketplace submission, Claude Code package,
install hook, or automated environment installer is part of this release.

## Verification

- Validate the manifest and skill with the available Codex/plugin validators. Check that skill
  instructions cover all five intents, read the returned stage contract, and include the error and
  trust rules above.
- Extend deterministic scripted MCP acceptance to cover the complete selected role order,
  reflection before analysis, evidence pagination, schema rejection and repair, retryable source
  failure, and restart/resume. Supply role outputs as fixtures; these checks prove protocol and
  state behavior, not investment quality or model instruction following.
- Run the clean installed-package protocol smoke outside the checkout for Python 3.10 through
  3.13, base-install checks, the full pytest suite, and `ruff check .`. Plugin fixtures fail
  immediately if the LLM-client factory is called.
- In a new installed Codex session, record desktop/CLI host versions and dates, skill and tool
  discovery, standalone data, one full real analysis without Python LLM API credentials, report
  and decision artifacts, and recovery of a separately interrupted run. The other host must pass
  discovery and recovery checks. Record missing sources or host limitations as results rather
  than presenting them as successful data.

## Expected files

- `plugins/tradingagents/.codex-plugin/plugin.json`
- `plugins/tradingagents/.mcp.json`
- `plugins/tradingagents/skills/trading-analysis/SKILL.md`
- `plugins/tradingagents/README.md`
- Focused protocol/skill checks under `tests/` or `scripts/`, and a dated installed-host record
  under `docs/`.

Runtime changes are limited to gaps demonstrated by these checks. Existing API-runner behavior,
storage formats, and role prompts remain owned by their current modules.
