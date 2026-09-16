# User testing US-005–US-008

Use this checklist to verify the implemented US-005–US-008 runtime.

This milestone exposes data and creates the first analyst stage. It does not run or advance a
complete analysis; stage submission arrives in US-009.

## 1. Prepare a development installation

From the repository root, activate a dedicated Python 3.10+ environment and run:

```bash
python -m pip install -e ".[dev,plugin]"
python -m pip check
python scripts/smoke_plugin_protocol.py
```

The protocol smoke test must pass before manual testing. It proves that the installed executable
starts outside the repository and keeps standard output protocol-only.

Data-provider credentials are optional:

- `ALPHA_VANTAGE_API_KEY` enables Alpha Vantage when selected.
- `FRED_API_KEY` enables FRED macro data.
- yfinance, Polymarket, StockTwits, and Reddit require no configured key but may still be
  unavailable or rate-limited.

No LLM API key is required for this milestone.

## 2. Register the local MCP server with Codex

With the development environment still active, create an isolated state directory and register
the installed executable:

```bash
PLUGIN_MCP="$(command -v tradingagents-mcp)"
PLUGIN_TEST_STATE="$(mktemp -d)"
codex mcp add tradingagents-test -- "$PLUGIN_MCP" --state-dir "$PLUGIN_TEST_STATE"
codex mcp get tradingagents-test
```

Keep that terminal open so `PLUGIN_TEST_STATE` remains available for the restart check. Start a
new Codex task after registration. If an existing server named `tradingagents-test` exists, remove
it with `codex mcp remove tradingagents-test` before adding the isolated test instance.

US-018 will provide packaged plugin activation. This checklist uses direct local MCP registration
because packaging is outside US-005–US-008.

## 3. Verify discovery

Ask Codex:

> Call TradingAgents `get_capabilities`. Show the available tool names and credential-presence
> flags, but do not display any credential values.

Pass when:

- `start_analysis`, `get_analysis`, and all fifteen data/identity tools are available.
- Each data source's `credential_present` field is a boolean or `null`; no credential value is
  returned.
- API-runner model settings are described as unrelated to the Codex plugin path.
- No decision-history, stage-submission, finalization, or reflection tool is claimed available.

## 4. Test standalone tools

### Instrument identity

Ask Codex to call `resolve_instrument_identity` for each input:

- `CNC.TO` must preserve the exchange suffix.
- `XAUUSD+` must resolve through the supported alias to `GC=F`.
- `BTCUSD` must resolve to `BTC-USD` with crypto context.

Each response should have `run_id=null`, `evidence_id=null`, and `reused=false`.

### Market and contextual data

Ask Codex:

> Call TradingAgents `get_verified_market_snapshot` as a standalone call with `symbol="AAPL"` and
> `curr_date="YYYY-MM-DD"` set to today's local date. Show `status`, `retryable`, `source`,
> `warnings`, and `page.complete`.

Then ask for one contextual source, such as ticker news or a macro indicator. Pass when:

- The response uses the common envelope.
- `status` is `success`, `no_data`, or `unavailable` for terminal outcomes; a transient failure
  has `status="error"` and `retryable=true`.
- Source content is returned without an LLM-generated replacement.
- Standalone responses have `page.complete=true` and `evidence_id=null`.
- Missing FRED configuration reports unavailable rather than inventing macro data.

Do not treat live-source availability as a deterministic pass condition. A rate limit or network
failure passes this check when it is honestly labeled retryable and no evidence ID is returned.

## 5. Create and inspect a market run

Generate a request UUID:

```bash
python -c 'import uuid; print(uuid.uuid4())'
```

Copy it into this request to Codex:

> Call TradingAgents `start_analysis` with `ticker="AAPL"`, `asset_type="stock"`,
> `analysts=["market"]`, `research_rounds=1`, `risk_rounds=1`, `output_language="English"`,
> `vendor_overrides={}`, and `request_id="<PASTE_UUID>"`. Omit `analysis_date` so the server
> resolves the host-local date. Show the complete result.

Pass when:

- `analysis_date` is the host-local calendar date.
- `status="active"`, `revision=1`, and `current_stage="analyst/market"`.
- `run_id` is a UUID, and `frozen_config`, `instrument`, and `lessons` are present.
- No LLM provider/model setting appears in `frozen_config`.

Copy the returned run UUID, then ask:

> Call TradingAgents `get_analysis` with `run_id="<RUN_UUID>"`. Do not call any data source.

Pass when the stored `status`, `revision`, `current_stage`, `frozen_config`, `instrument`, and
`lessons` match the creation response and the read causes no network activity.

## 6. Test run-bound evidence and reuse

Ask Codex to call `get_verified_market_snapshot` with `run_id=<RUN_UUID>`, `symbol` set to the
frozen canonical ticker, and `curr_date` set to the exact echoed `analysis_date`.

Pass when the response contains:

- `run_id=<RUN_UUID>` and `stage_id="analyst/market"`.
- A non-null `evidence_id` for a terminal result.
- `reused=false` on the first terminal call.
- Explicit source and warnings, using `unknown` rather than guessing when necessary.

Repeat the identical call. Pass when:

- `reused=true`.
- `evidence_id`, `fetched_at`, `status`, `content`, `source`, and `warnings` are unchanged.
- The source is not fetched again.

Call `get_analysis` again. Its evidence metadata must include the saved record without refetching
it.

## 7. Test paging

Make a run-bound market-data call likely to return a large range and set `page_size=500`.

Pass when:

- A partial response has `page.complete=false` and a non-null `page.next_cursor`.
- Repeating the same `run_id`, tool, domain arguments, and `page_size` with that cursor returns the
  next saved page.
- Joining all pages reconstructs the complete saved content without gaps or overlap.
- A malformed cursor or a cursor used with different arguments is rejected.
- Supplying a cursor or page size to a standalone call is rejected.

## 8. Test safety failures

Using the market run, try each invalid operation separately:

1. Call `get_news` with the run ID. It must fail because news is not permitted in
   `analyst/market`.
2. Call a permitted market tool with a different ticker. It must fail before fetching.
3. Call a point-in-time market tool with a date different from the frozen analysis date. It must
   fail before fetching.
4. Call ranged market data with an end date after the analysis date. It must fail before fetching.
5. Repeat `start_analysis` with the same request UUID and identical normalized inputs. It must
   return the same run.
6. Repeat `start_analysis` with the same request UUID but a changed analyst selection. It must
   return `REQUEST_ID_CONFLICT` and create no run.
7. Try an empty analyst list, duplicate analysts, an invalid date, round count zero, and an unknown
   vendor. Each must fail before run creation.

## 9. Test the other first-stage permissions

Create fresh runs with new request UUIDs and one analyst each:

| Analyst | Expected stage | Allowed checks |
|---|---|---|
| `social` | `analyst/social` | ticker news, StockTwits, Reddit |
| `news` | `analyst/news` | ticker/global news, insider transactions, macro, prediction markets |
| `fundamentals` | `analyst/fundamentals` | fundamentals and three financial statements |

For every run, verify one allowed call succeeds or returns an honest source status and one tool
from another row is rejected before fetching. Historical insider and prediction-market calls must
include a historical-coverage warning rather than claim point-in-time cleanliness.

## 10. Test restart persistence

Quit the Codex task, remove and re-add the MCP entry with the same executable and state directory,
then start a new task:

```bash
codex mcp remove tradingagents-test
codex mcp add tradingagents-test -- "$PLUGIN_MCP" --state-dir "$PLUGIN_TEST_STATE"
```

Ask Codex to call `get_analysis` with the earlier run UUID and repeat an earlier evidence request.

Pass when:

- The run retains its `status`, `revision`, `current_stage`, `frozen_config`, `instrument`, and
  `lessons`.
- Evidence metadata and content are unchanged.
- The repeated evidence call has `reused=true` and does not contact the source.

## 11. Run the automated acceptance checks

Manual testing cannot reliably prove concurrent configuration isolation. Run the focused tests:

```bash
pytest tests/test_dataflows_config.py tests/test_vendor_routing.py \
  tests/test_vendor_errors.py tests/test_no_data_handling.py \
  tests/test_plugin_store.py tests/test_plugin_data.py \
  tests/test_plugin_capabilities.py tests/test_plugin_runtime.py \
  tests/test_symbol_utils.py tests/test_ticker_symbol_handling.py \
  tests/test_fred.py tests/test_polymarket.py tests/test_stocktwits_resilience.py \
  tests/test_reddit_fallback.py tests/test_social_lookahead.py -q
pytest -q
ruff check .
```

Pass only when all commands succeed. The focused suite must include two runs with different vendor
configurations, one failing fetch, and proof that the complete prior configuration is restored.

## Record the result

Record:

- Date, operating system, Python version, and Codex version.
- Commit tested.
- Whether Alpha Vantage and FRED were configured, without recording secrets.
- Passed and failed sections above.
- Run UUIDs and evidence UUIDs needed to reproduce a failure.
- Any source outage or rate limit separately from a product defect.

Do not record API keys, full environment dumps, or private evidence content.
