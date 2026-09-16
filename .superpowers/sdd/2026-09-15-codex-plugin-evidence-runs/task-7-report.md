# Task 7 Report

## Status

DONE

## Implemented

- Replaced the obsolete single-tool runtime documentation with the implemented discovery,
  lifecycle, and fifteen typed data/identity tools.
- Documented the SQLite database location, restart persistence, UUID request idempotency,
  frozen run state, first-stage-only permissions, terminal evidence reuse, retryable-error
  behavior, paging, network-free stored reads, and the absence of an LLM credential requirement.
- Kept host manifest installation and packaged activation explicitly deferred to US-018.
- Activated the US-005–US-008 user-testing guide and aligned examples with the final argument and
  response field names.
- Expanded the guide's automated acceptance command to the complete focused test set.
- Added `tests/test_plugin_store.py` and `tests/test_plugin_data.py` to the existing CI plugin job,
  as required by the brief after inspection showed they were absent.
- Made no runtime-code changes.

## Files changed

- `docs/plugin-runtime.md`
- `docs/user-testing-us-005-008.md`
- `.github/workflows/ci.yml`

## Validation

- Baseline full suite before edits: `820 passed, 2 skipped, 18 warnings, 73 subtests passed`.
- Required focused acceptance set: `207 passed in 1.84s`.
- Updated CI plugin-job local equivalent: `137 passed in 1.84s`.
- Fresh non-editable `.[plugin]` install: passed on Python 3.12.13.
- Fresh-environment `pip check`: `No broken requirements found.`
- Installed protocol smoke from outside the checkout: passed.
- Full repository suite after final edits: `820 passed, 2 skipped, 18 warnings, 73 subtests
  passed in 3.44s`.
- Ruff: `All checks passed!`.
- Wheel build: built `tradingagents-0.4.0-py3-none-any.whl` successfully.
- `git diff --check` and staged `git diff --cached --check`: passed.
- Pre-commit status contained only the intended two docs and CI workflow; post-commit status is
  clean.

The two full-suite skips are existing environment/API conditions: missing optional
`langchain_aws` and an unset `DEEPSEEK_API_KEY`.

## Final acceptance checklist

- All fifteen data/identity tools are registered, advertised, and covered by typed MCP discovery.
- Existing API data return and exception behavior is covered by the focused dataflow/vendor tests.
- Start requests are validated, frozen, restart-safe, and UUID-idempotent.
- Only the first selected analyst stage is available; sibling-stage tools are rejected.
- Terminal evidence is saved once and reused; retryable errors are not saved.
- Paging reconstructs saved content and rejects malformed or mismatched cursors.
- Concurrent differently configured runs remain isolated, including after a failing fetch.
- `get_analysis` and saved-evidence continuation do not access the network.
- Plugin validation proves no LLM client is constructed.
- Focused tests, CI plugin equivalent, clean install, `pip check`, installed protocol smoke, full
  pytest, Ruff, and wheel build all pass.

## Self-review

- Confirmed the runtime guide names exactly two lifecycle and fifteen data/identity tools.
- Confirmed user-testing examples use final names including `symbol`, `curr_date`, `run_id`,
  `stage_id`, `evidence_id`, `fetched_at`, and nested `page` fields.
- Confirmed the guide retains direct `codex mcp add` setup and clearly defers US-018 activation.
- Confirmed the CI change adds only the two focused modules required by the brief.
- Confirmed no runtime, test, dependency, or unrelated file changed.

## Commit

`c53dd8b docs: document plugin evidence testing`

## Concerns

- The installed protocol smoke emitted an upstream Pydantic settings warning on stderr; it exited
  zero and protocol stdout remained valid.
- Git inferred the commit author from the local username and hostname; the commit succeeded and
  was not amended.

## Final-review fix wave — 2026-09-16

### Findings fixed

1. Added shared vendor-diagnostic redaction for environment-backed secrets, credential-shaped
   values, bearer/basic credentials, and URLs. Router warnings, optional failure sentinels,
   no-data details, plugin error envelopes, and persisted warnings are sanitized. Legacy
   `route_to_vendor()` exceptions remain the original exception objects and messages.
2. Classified provider failure sentinels (`Error fetching`, `Error retrieving`, `Error:`, network
   unavailable markers, StockTwits/Reddit failures, and Alpha Vantage diagnostic JSON) as retryable
   plugin errors. They are neither persisted nor reused; regressions cover repeated calls.
3. Alpha Vantage now inspects `Error Message` as well as `Information`/`Note`, marking a configured
   invalid/expired/unauthorized key with `authentication_failed=True`. Missing environment
   configuration remains terminal unavailable; invalid configured auth is retryable.
4. Run-bound StockTwits and Reddit calls require a complete explicit start/end window. Standalone
   calls retain their optional date signatures.
5. MCP tools are registered with forbidden top-level extras at the FastMCP boundary. Regressions
   cover LLM settings, path settings, and argument typos before identity/network/database effects.
6. Optional rate-limit-only exhaustion returns a legacy `RuntimeError` through
   `route_to_vendor()`, while `route_to_vendor_traced()` remains `status=error`, retryable.

### Changed files

- `tradingagents/dataflows/alpha_vantage_common.py`
- `tradingagents/dataflows/errors.py`
- `tradingagents/dataflows/interface.py`
- `tradingagents/plugin/data.py`
- `tradingagents/plugin/server.py`
- `tests/test_alpha_vantage_hardening.py`
- `tests/test_plugin_capabilities.py`
- `tests/test_plugin_data.py`
- `tests/test_plugin_runtime.py`
- `tests/test_vendor_routing.py`

### Validation output

- Focused acceptance set: `159 passed, 1 warning in 1.65s`.
- Full repository suite: `851 passed, 2 skipped, 19 warnings, 73 subtests passed in 3.47s`.
- Full Ruff: `All checks passed!`.
- `git diff --check`: passed.
- Existing skips: optional `langchain_aws` unavailable and live DeepSeek API test skipped without
  `DEEPSEEK_API_KEY`.
- Existing warnings include upstream Pydantic settings forward-reference output and test fixtures
  intentionally exercising unknown/unsupported model names.

### Self-review

- Verified all six findings have focused regressions and shared-path coverage.
- Verified no credential-bearing diagnostic is returned by the exercised plugin error paths or
  saved evidence; legacy exception identity remains covered.
- Verified strict MCP validation runs before instrument resolution and run insertion.
- Verified the worktree diff contains no dependency, CI, or unrelated documentation changes.
