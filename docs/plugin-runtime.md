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

The optional SDK is pinned to `mcp==1.26.0`. The runtime exposes capability discovery, two run
lifecycle tools, and fifteen typed data/identity tools:

- Discovery: `get_capabilities`.
- Lifecycle: `start_analysis`, `get_analysis`.
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
without network access.

Only the first selected analyst stage is active in this milestone. Run-bound calls reject tools,
symbols, and date windows outside that frozen stage. Stage submission, later-stage progression,
finalization, decision history, and reflection are not available yet.

Data tools return `status`, `content`, `content_format`, `source`, `warnings`, `fetched_at`,
`reused`, `retryable`, evidence/run/stage IDs, and a `page` object. Standalone calls are not saved.
For run-bound calls, terminal `success`, `no_data`, and `unavailable` results are saved once and
reused. Retryable failures use `status="error"`, `retryable=true`, and no evidence ID; they are not
saved, so a repeated request fetches again. Saved content can be continued with `cursor` and
`page_size`; continuation reads the database rather than the source.

Host manifest installation and packaged activation remain explicitly out of scope until US-018;
use direct executable launch or `codex mcp add` in the meantime.
