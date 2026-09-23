# TradingAgents MCP runtime

Use an activated, dedicated Python environment. Install the optional runtime and launch it with:

```bash
python -m pip install ".[plugin]"
python -m pip check
tradingagents-mcp --state-dir "$HOME/.tradingagents/plugin"
```

The direct launch waits for an MCP client on standard input. An empty terminal that remains open
is not a completed analysis. The server writes only MCP protocol messages to standard output;
diagnostics and logs use standard error.

By default, state is stored at `~/.tradingagents/plugin/`. `--state-dir` takes precedence over
`TRADINGAGENTS_PLUGIN_STATE_DIR`. The runtime creates and verifies that directory is writable and
initializes its SQLite state database at `<state-dir>/plugin.sqlite3`. Runs and terminal evidence
survive server restarts when the same state directory is reused.

`ALPHA_VANTAGE_API_KEY` and `FRED_API_KEY` are optional. `get_capabilities` reports only whether
they are present, never their values. Keyless sources (yfinance, Polymarket, StockTwits, and
Reddit) can still be unavailable or rate-limited; a present key does not establish source
availability.

No LLM API credentials are needed for this runtime milestone. The plugin freezes only data,
language, and round settings and does not construct an LLM client. API-runner model settings do
not configure the Codex session that reasons over returned evidence.

The package inherits its `.env` lookup from `python-dotenv`: it searches relative to the launch
working directory and its ancestors. For launches outside the checkout, export settings in the
environment instead of depending on a checkout-local `.env` file.

The optional SDK is pinned to `mcp==1.26.0`. The runtime exposes capability discovery, five run
lifecycle tools, and fifteen typed data/identity tools:

- Discovery: `get_capabilities`.
- Lifecycle: `start_analysis`, `get_analysis`, `submit_stage`, `list_analyses`, and
  `cancel_analysis`.
- Market and fundamentals: `get_stock_data`, `get_indicators`,
  `get_verified_market_snapshot`, `get_fundamentals`, `get_balance_sheet`, `get_cashflow`,
  `get_income_statement`.
- Identity and context: `resolve_instrument_identity`, `get_news`, `get_global_news`,
  `get_insider_transactions`, `get_macro_indicators`, `get_prediction_markets`.
- Social: `fetch_stocktwits_messages`, `fetch_reddit_posts`.

`start_analysis` requires a UUID `request_id`, validates and normalizes the request, and freezes
the resolved date, instrument identity, data configuration, analyst order, and lessons. Repeating
the same UUID with the same normalized request returns the original run; reusing it for a different
request returns `REQUEST_ID_CONFLICT`. `get_analysis` reads the stored run and evidence metadata
without network access. It also returns the active role, complete prompt, required-evidence
checklist, output schema, and accepted section receipts. `list_analyses` finds persisted runs after
a restart and supports ticker and status filters plus cursor pagination.

For each active stage, read `get_analysis`, fetch any required evidence through the run-bound data
tools, reason over the returned prompt, then call `submit_stage` with the current `run_id`,
`stage_id`, and `expected_revision`. Narrative analyst, bull, bear, and risk outputs must be native
nonempty strings. Sentiment, research-manager, trader, and portfolio outputs must be native JSON
objects matching the returned schema. Canonical submissions are limited to 32,000 characters.
Do not wrap either output kind in Markdown fences or stringify an object.

Market analysis requires a saved `get_verified_market_snapshot` attempt for the frozen symbol and
analysis date before submission. Sentiment analysis requires saved terminal attempts from
`get_news`, `fetch_stocktwits_messages`, and `fetch_reddit_posts` for the exact seven-day window
shown by `get_analysis`. A terminal `success`, `no_data`, or `unavailable` result satisfies the
gate; retryable errors are not saved and must be retried.

Every accepted submission increments the run revision and advances exactly one stage. Retry an
identical already-accepted payload to recover its original receipt; a different retry is rejected.
Use the latest revision for the next submission or for `cancel_analysis`. Cancellation preserves
evidence and outputs, is idempotent once recorded, and permanently rejects later fetches and
submissions for that run. Start a new run to resume work after cancellation.

After the portfolio stage, the run becomes `ready_to_finalize` at the `finalize` sentinel.
`finalize` is not a submittable stage. Call `finalize_analysis` to write the existing per-section
Markdown reports and consolidated report under `<results_dir>/plugin/<run_id>/`, append the
decision to memory using the run UUID as its stable identity, then record the export receipt and
mark the run complete. A retry first checks for that receipt. If a previous attempt wrote memory
but failed before recording completion, the stable decision identity prevents a duplicate memory
entry and the export can be retried.

Data tools return `status`, `content`, `content_format`, `source`, `warnings`, `fetched_at`,
`reused`, `retryable`, evidence/run/stage IDs, and a `page` object. Standalone calls are not saved.
For run-bound calls, terminal `success`, `no_data`, and `unavailable` results are saved once and
reused. Retryable failures use `status="error"`, `retryable=true`, and no evidence ID; they are not
saved, so a repeated request fetches again. Saved content can be continued with `cursor` and
`page_size`; continuation reads the database rather than the source.

Host manifest installation and packaged activation remain explicitly out of scope until US-018;
use direct executable launch or `codex mcp add` in the meantime.

## Decision history and reflections

`get_decision_history` is read-only and supports ticker and `as_of_date` filters with cursor
pagination. The Markdown memory log gives plugin-created decisions their run UUID as a canonical
identity; older entries without an identity are resolved from their date, ticker, rating, and
decision text, and ambiguous duplicate legacy identities are reported rather than guessed. Memory
mutations use a sibling lock file across processes and an atomic replacement, preserving concurrent
plugin and existing-runner writes.

An as-of history view omits decisions after the cutoff and projects a decision as pending when its
outcome was resolved after the cutoff. New analysis receives only lessons whose resolution date is
on or before its analysis date. This prevents later outcomes and reflections from leaking into
historical views or prompts.

`prepare_reflections(ticker, as_of_date)` creates or reuses jobs for eligible pending decisions.
An outcome is available only after the full five-session holding period; the job records raw return,
benchmark-relative return, the five-session period, benchmark identity, and resolution date. If the
window is incomplete or unavailable, the decision remains pending. Read a job with `get_analysis`,
submit Codex's nonempty reflection for its `reflection` stage using the current revision, then call
`finalize_analysis`. The runtime builds the existing concise reflection prompt but never invokes an
LLM; finalization updates the identified decision once and is retryable.

`start_analysis` accepts `skip_reflections`. When true, it omits prior lessons and returns
`learning_omitted=true` in the run result. Automatic preparation and completion of reflections
before a new analysis is intentionally deferred to US-017's skill orchestration; the runtime does
not launch that workflow itself.
