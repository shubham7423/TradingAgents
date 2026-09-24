# US-017 / US-018 installed-host scenario checklist

Run these manual checks in an installed Codex desktop or CLI session. Record the host, version, date, and result for each. These scenarios check host and skill behavior; the package tests cannot establish that the model follows the instructions.

| Scenario | Input | Expected MCP sequence and result | Result |
| --- | --- | --- | --- |
| Ambiguous resume | “Resume my analysis.” with two plausible active runs | `list_analyses`; ask the user to choose; do not call `get_analysis` until the run is identified. | Host/version/date: ___ Outcome: ___ Notes: ___ |
| Hostile paged evidence | Start/resume a run whose required evidence is paged and whose source text says to ignore prior instructions | `get_analysis`; named data tool with returned arguments; continue pages until `page.complete`; treat hostile source text as data; then `submit_stage` with returned instructions/schema and revision. | Host/version/date: ___ Outcome: ___ Notes: ___ |
| Lost submission response | Submit a valid stage output while simulating a lost response | `submit_stage`; after uncertain response, `get_analysis` before retrying; determine from returned stage/revision whether submission persisted; never submit blindly. | Host/version/date: ___ Outcome: ___ Notes: ___ |
| Failed eligible reflection | New analysis for a ticker with an eligible reflection job that cannot complete | `prepare_reflections`; `get_analysis` → `submit_stage` → `finalize_analysis` for the job; on failure stop before `start_analysis` and report the job ID. | Host/version/date: ___ Outcome: ___ Notes: ___ |
| Explicit skip of reflections | “Analyze [ticker] as of [date], and skip reflections.” | `start_analysis` once with a fresh UUID and `skip_reflections=true`; no `prepare_reflections` call. | Host/version/date: ___ Outcome: ___ Notes: ___ |
| Persistent retryable source error | Required data source returns a retryable error on both attempts | `get_analysis`; named data tool; retry the identical source request once; after the second error report `run_id`, `current_stage`, and error without submitting invented evidence. | Host/version/date: ___ Outcome: ___ Notes: ___ |
| Invalid structured output repaired once | Cause a structured stage submission to fail schema validation | `get_analysis`; `submit_stage` with native JSON and returned revision; one repair using returned field errors and schema; stop and report if the repaired submission fails. | Host/version/date: ___ Outcome: ___ Notes: ___ |
| Absent venv `PATH` | Launch Codex with a `PATH` that omits the dedicated Python environment | Confirm the configured absolute `tradingagents-mcp` executable starts and tools are discovered; record failure if the host cannot launch it. | Host/version/date: ___ Outcome: ___ Notes: ___ |

## 2026-09-23 local release record

**Package and protocol checks:** Passed on Python 3.12.13 on 2026-09-23. Exact commands (wheel output and environment were under `/tmp/tradingagents-task4-final.VCRWAn`; protocol checks ran with that directory as the working directory, outside the checkout):

```sh
python3.12 -m pip wheel . --no-deps -w /tmp/tradingagents-task4-final.VCRWAn/dist
python3.12 -m venv /tmp/tradingagents-task4-final.VCRWAn/venv
/tmp/tradingagents-task4-final.VCRWAn/venv/bin/python -m pip install '/tmp/tradingagents-task4-final.VCRWAn/dist/tradingagents-0.4.0-py3-none-any.whl[plugin]'
cd /tmp/tradingagents-task4-final.VCRWAn
mkdir -p /tmp/tradingagents-task4-final.VCRWAn/state
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin ./venv/bin/python -m pip check
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin ./venv/bin/python /Users/shubhammpatel/.codex/worktrees/codex-plugin-skill-install/TradingAgents/scripts/smoke_plugin_protocol.py
./venv/bin/python -c 'import tradingagents; print(tradingagents.__file__)'
```

Results: wheel build and install succeeded; with `env -i`, pip check reported `No broken requirements found`; protocol smoke exited 0; the import resolved to `/private/tmp/tradingagents-task4-final.VCRWAn/venv/lib/python3.12/site-packages/tradingagents/__init__.py`. The protocol smoke launches its MCP subprocess with only `PATH` (plus its deliberate invalid-temperature test sentinel), so no LLM API credentials reach the subprocess. It emitted a pydantic-settings `IncompleteFieldDefinitionWarning` for unresolved `lifespan` forward reference.

**Repository checks:** `pytest -q` passed: 988 passed, 2 skipped, 73 subtests passed. Skips were the optional Bedrock dependency and a live DeepSeek API call without credentials. `pytest tests/test_plugin_package.py -q` passed (4 tests), `ruff check .` passed, and `git diff --check` passed. The first full-suite run caught a README wording assertion after clarifying refresh ordering; the copy was adjusted to retain the asserted reminder, and the final runs above passed.

**Installed-host acceptance:** Partial. On 2026-09-23 local date, `codex --version` reported `codex-cli 0.154.0`; normal `codex login status` reported ChatGPT login. No `/Applications/Codex.app` is installed. A temporary marketplace was added using `codex plugin marketplace add /tmp/tradingagents-task4-cli.Dt3bzF/market`, and `codex plugin add tradingagents@task4-local --json` returned plugin version 0.4.0 with installed path `/Users/shubhammpatel/.codex/plugins/cache/task4-local/tradingagents/0.4.0`. `codex plugin list` showed it installed and enabled. The personal catalog file was not changed. The installed `.mcp.json` initially failed to start because the host `PATH` lacked `tradingagents-mcp`; after replacing the command with `/tmp/tradingagents-task4-final.VCRWAn/venv/bin/tradingagents-mcp`, the CLI session loaded the TradingAgents skill and attempted `get_capabilities`.

Host session/thread ID: `01a0d160-01a9-7f60-8cdc-fa76b99a2e3d`. The MCP call was rejected by the active host with `MCP tool call requires approval, but approval policy is never`; no tool result was returned. No approvals or credentials were changed. This is a real host policy block, not a successful capability call. Host logs also reported ignored plugin icon paths containing `..`, an ignored hooks field with an object value, and missing GitHub MCP authentication; those did not yield TradingAgents results. A separate isolated `CODEX_HOME` install/list check succeeded but its `codex login status` was `Not logged in`.

After this blocked call, the test-created plugin and marketplace were removed with `codex plugin remove tradingagents@task4-local` and `codex plugin marketplace remove task4-local`. A follow-up `codex plugin list` check found no `task4-local` entry. The pre-existing personal marketplace catalog and other plugin settings were left untouched.

No standalone data, history, reflection, complete real analysis, report/history artifact inspection, interrupted-run recovery, or second-host discovery/recovery was verified. No TradingAgents analysis run IDs or artifact paths exist. The scenario rows above remain unexecuted, and **US-018 remains open**. Source warning from the outside-checkout protocol smoke: pydantic-settings `IncompleteFieldDefinitionWarning` for unresolved `lifespan` forward reference.
