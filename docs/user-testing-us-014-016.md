# User testing: US-014–016

Automated checks run on 2026-09-23 in the project `.venv` (Python 3.13). Manual checks are
listed separately and remain pending until performed on an installed host or with separate
processes as described.

## Automated checks

| Command | Result |
| --- | --- |
| `.venv/bin/pytest tests/test_reporting.py tests/test_memory_log.py tests/test_memory_pointintime.py -q` | PASS — 96 passed in 1.22s. |
| `.venv/bin/pytest tests/test_plugin_store.py tests/test_plugin_workflow.py tests/test_plugin_data.py -q` | PASS — 208 passed in 0.86s. |
| `.venv/bin/pytest tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q` | PASS — 16 passed in 1.31s; one Pydantic settings warning. |
| `.venv/bin/pytest -q` | PASS — 979 passed, 2 skipped, 19 warnings, 73 subtests passed in 3.93s. |
| `.venv/bin/ruff check .` | PASS — All checks passed! |
| `.venv/bin/python -m pip wheel . --no-deps -w /tmp/tradingagents-dist` | BLOCKED — the worktree's `.venv/bin/python` has no `pip` module. |
| `python3 -m pip wheel . --no-deps -w /tmp/tradingagents-dist` | PASS — built `tradingagents-0.4.0-py3-none-any.whl` (208828 bytes). |
| `git diff --check` | PASS — no whitespace errors. |

The two skipped full-suite tests were the optional `langchain_aws` test (dependency unavailable)
and a live DeepSeek API test (no API key). The initial full-suite run exposed a stale test patch
target after outcome fetching moved to `tradingagents.graph.reflection`; the test now patches that
shared helper's yfinance handle, and the complete suite passes.

## Manual checks

| Check | Result slot |
| --- | --- |
| Restart after memory export: restart the server with the same state and memory paths; confirm the completed run, artifact receipt, and history entry remain available. | **Not run — pending.** Date: ____ Result: ____ |
| Two-process history preservation: have separate processes append decisions to one memory file concurrently; confirm both entries remain. | **Not run — pending.** Date: ____ Result: ____ |
| Legacy entry reflection: prepare, submit, and finalize a reflection for a uniquely identifiable legacy memory entry; confirm its decision is updated once. | **Not run — pending.** Date: ____ Result: ____ |
| History cutoff: query a historical `as_of_date` before a later outcome's resolution; confirm the outcome and reflection are projected as pending/absent. | **Not run — pending.** Date: ____ Result: ____ |
| Installed stdio discovery: from a clean installed environment, launch the stdio server and confirm MCP initialization and tool discovery without LLM credentials. | **Not run — pending.** Date: ____ Host/version: ____ Result: ____ |
