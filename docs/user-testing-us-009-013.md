# User testing: US-009 through US-013

Verification date: 2026-09-19 (America/Toronto)

This milestone proves the durable staged workflow through `ready_to_finalize`. It does not export
reports or write decision memory; those capabilities begin in US-014.

## Automated checks

- [x] Complete workflow and SDK lifecycle

  Command:

  ```bash
  ../../.venv/bin/python -m pytest \
    tests/test_plugin_workflow.py::test_complete_multi_round_run_survives_restarts \
    tests/test_plugin_runtime.py::test_sdk_executes_and_cancels_workflow -q
  ```

  Observed output: `2 passed, 1 warning in 0.51s`. The warning was Pydantic's
  `IncompleteFieldDefinitionWarning` for FastMCP's `lifespan` annotation.

- [x] Focused workflow regressions

  Command:

  ```bash
  ../../.venv/bin/python -m pytest tests/test_plugin_store.py tests/test_plugin_workflow.py \
    tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py \
    tests/test_shared_analyst_prompts.py tests/test_shared_synthesis.py \
    tests/test_structured_agents.py tests/test_structured_agent_prompts.py \
    tests/test_signal_processing.py -q
  ```

  Observed output: `299 passed, 1 warning in 1.93s`. The warning was the same FastMCP/Pydantic
  forward-reference warning.

- [x] Full test suite

  Command: `../../.venv/bin/python -m pytest -q`

  Observed output after the final review fix: `944 passed, 2 skipped, 19 warnings, 73 subtests
  passed in 3.81s`.

- [x] Ruff

  Command: `../../.venv/bin/ruff check .`

  Observed output: `All checks passed!`

- [x] Wheel build

  Command: `../../.venv/bin/python -m pip wheel . --no-deps -w /tmp/tradingagents-dist`

  Observed output: `Successfully built tradingagents`. Created
  `tradingagents-0.4.0-py3-none-any.whl` (203190 bytes), SHA-256
  `88434cd8d0d26fcb73245c23641d221c5c6cc5df419eca5c3f812ab0b5b0de32`.

## Local stdio discovery and call

- [x] Launch `tradingagents.plugin.server` over stdio, discover its tools, and call
  `get_capabilities` without a vendor or model request.

  Command: local stdio `initialize`, `tools/list`, then `tools/call(get_capabilities)`.

  Observed output: `tools=21 get_capabilities=ok stderr_bytes=455`. No vendor or model was called.
