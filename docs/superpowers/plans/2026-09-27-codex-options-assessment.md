# Codex Options Assessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an automatically discoverable `options-trading` skill that reuses completed Codex stock research and returns a concise, quote-backed cash-secured put or long-call assessment.

**Architecture:** Keep the current stock workflow unchanged. A small options module retrieves current yfinance chains, the plugin store saves quote snapshots and one assessment per completed run, and MCP tools expose those operations. Python checks selected contracts and calculates per-contract economics; the new skill coordinates research and presents the short memo.

**Tech Stack:** Python 3.10+, existing yfinance, Pydantic, stdlib SQLite, MCP SDK optional extra, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-27-codex-options-assessment-design.md`

## Global Constraints

- Current, completed `asset_type="stock"` plugin runs only; reject historical analysis dates and crypto. No historical option-chain reconstruction.
- Standard USD equity/ETF contracts with verified 100-share terms only. Quote source and fetch time must be visible; last trade time is not quote time.
- Cash-secured put uses bid, collateral `strike * 100`, credit `bid * 100`, breakeven `strike - bid`, worst-case loss `(strike - bid) * 100`; long call uses ask, debit/max loss `ask * 100`, breakeven `strike + ask`. Figures exclude fees and do not promise fills.
- One assessment per run. Identical submission replays; a conflicting submission fails. A new run is needed to refresh quotes.
- Preserve the existing stock report, decision log, reflection path, and API-backed CLI. Do not place orders or create LLM clients in Python.
- Keep the pre-existing modified `README.md` and staged `.superpowers/brainstorm/` files out of task commits.

## File map

| File | Responsibility |
|---|---|
| `tradingagents/plugin/options.py` (new) | Fetch/normalize option chains; validate selected contracts; calculate and render the memo. |
| `tradingagents/plugin/store.py` | Migrate SQLite schema; save and read immutable per-run/expiration snapshots and one assessment receipt. |
| `tradingagents/plugin/data.py` | Typed MCP-facing options methods, run/date checks, pagination, and result schemas. |
| `tradingagents/plugin/server.py` | Advertise the options tools in capabilities; existing `public_operations()` registration publishes them. |
| `plugins/tradingagents/skills/options-trading/SKILL.md` (new) | Route options intent; reuse `trading-analysis` for stock research; select/submit contracts and show a concise result. |
| `plugins/tradingagents/README.md`, `docs/plugin-runtime.md` | Explain activation, supported instruments, current quotes, and example requests. |
| `tests/test_plugin_options.py` (new), existing plugin test modules | Pin arithmetic, quote handling, persistence, tool schema, and skill routing. |

## Review Focus

1. A completed stock run created yesterday must reject today's chain rather than pairing mismatched evidence. Task 3 tests this.
2. A provider returning `NaN`, zero, crossed, or missing bid/ask must never produce a trade candidate. Tasks 1 and 4 test this.
3. An adjusted contract that reports a 100-share-looking quote must still fail when its option symbol root or size differs. Task 4 tests this.
4. A process crash after saving a receipt but before writing `options_memo.md` must be recoverable by identical replay. Task 4 tests this.
5. An ordinary “analyze AAPL stock” request must continue to route to `trading-analysis`, while indirect options requests find `options-trading`. Task 5 tests this.

---

### Task 1: Retrieve and normalize option chains

**Files:** Create `tradingagents/plugin/options.py`; create `tests/test_plugin_options.py`.

**Interfaces:**
- Produces `fetch_expirations(ticker: str) -> tuple[str, ...]` and `fetch_chain(ticker: str, expiration: str) -> dict`. `fetch_chain` returns `{"expiration": str, "underlying": dict, "contracts": list[dict]}` with each contract carrying `contract_symbol`, `side` (`put`/`call`), `strike`, `bid`, `ask`, `volume`, `open_interest`, `implied_volatility`, `currency`, `contract_size`, and `last_trade_at`.
- Normalize pandas and numpy scalar values to JSON-safe Python scalars. Missing optional fields become `None`; do not synthesize a quote from `lastPrice`.

- [ ] **Step 1: Write the failing fixture test.** Mock `yf.Ticker` with `options=("2026-10-16",)` and a chain whose `.puts`/`.calls` are small pandas DataFrames. Assert one put and one call retain exact bid/ask, currency, `contractSize`, and UTC last-trade time; assert missing optional volume is `None` and returned values round-trip through `json.dumps`.

```python
def test_fetch_chain_preserves_bid_ask_and_metadata(monkeypatch):
    from types import SimpleNamespace
    import pandas as pd

    put = {"contractSymbol": "AAPL261016P00150000", "strike": 150.0,
           "bid": 2.1, "ask": 2.3, "contractSize": "REGULAR", "currency": "USD",
           "lastTradeDate": pd.Timestamp("2026-09-27T14:00:00Z")}
    fake = SimpleNamespace(
        options=("2026-10-16",),
        option_chain=lambda expiry: SimpleNamespace(
            puts=pd.DataFrame([put]), calls=pd.DataFrame(), underlying={"regularMarketPrice": 155.0}
        ),
    )
    monkeypatch.setattr(options.yf, "Ticker", lambda ticker: fake)
    result = options.fetch_chain("AAPL", "2026-10-16")
    assert result["contracts"][0]["bid"] == 2.1
    assert result["contracts"][0]["contract_size"] == "REGULAR"
    json.dumps(result)
```

- [ ] **Step 2: Run red.** `pytest tests/test_plugin_options.py::test_fetch_chain_preserves_bid_ask_and_metadata -q` must fail because the module/function is missing.
- [ ] **Step 3: Implement only the adapter.** Use `yf.Ticker(ticker).options` and `.option_chain(expiration)`. Map DataFrame rows through a single conversion function; reject an expiration absent from the provider's listed set. Keep network/provider exceptions visible to the caller so MCP can return `unavailable` rather than invented rows.

```python
def fetch_chain(ticker: str, expiration: str) -> dict:
    stock = yf.Ticker(ticker)
    if expiration not in stock.options:
        raise ValueError(f"expiration {expiration} is unavailable for {ticker}")
    chain = stock.option_chain(expiration)
    return {
        "expiration": expiration,
        "underlying": dict(chain.underlying or {}),
        "contracts": [*_rows(chain.puts, "put"), *_rows(chain.calls, "call")],
    }
```

- [ ] **Step 4: Add one unavailable/empty-chain test, run `pytest tests/test_plugin_options.py -q`, then commit only this task's files.** Do not fetch live Yahoo data in tests.

### Task 2: Persist quote snapshots and assessment receipts

**Files:** Modify `tradingagents/plugin/store.py`, `tradingagents/plugin/server.py`, `tests/test_plugin_store.py`, and `tests/test_plugin_capabilities.py`.

**Interfaces:**
- Produces `save_option_snapshot(run_id: str, expiration: str, fetched_at: str, payload: dict) -> dict`, `get_option_snapshot(snapshot_id: str) -> dict`, `list_option_snapshots(run_id: str) -> list[dict]`, `save_option_assessment(run_id: str, submission_hash: str, result: dict, memo_path: str, now: str) -> tuple[dict, bool]`, and `get_option_assessment(run_id: str) -> dict | None`.
- Snapshot IDs are UUIDs; unique `(run_id, expiration)` freezes the first fetched chain for that run. Assessment result stores rendered memo text as well as structured calculations so file creation can be retried.

- [ ] **Step 1: Write failing migration and replay tests.** Create a v3 store with a completed run; reopen after migration; save a snapshot and assessment; reopen again and assert the same content. A second identical assessment must return `created=False`; a different `submission_hash` must raise `RequestIdConflict` or a dedicated conflict error. Two concurrent writers must leave one canonical receipt.

```python
run_id = ready_run(store).run_id
result = {"memo": "No trade: no usable quotes."}
memo_path = f"/results/plugin/{run_id}/options_memo.md"
now = "2026-09-27T12:00:00+00:00"
first, created = store.save_option_assessment(run_id, "same-hash", result, memo_path, now)
again, repeated = PluginStore(tmp_path).save_option_assessment(
    run_id, "same-hash", result, memo_path, now
)
assert created and not repeated and first == again
```

- [ ] **Step 2: Run red.** `pytest tests/test_plugin_store.py -q` must show the new tests failing on missing methods/schema.
- [ ] **Step 3: Add schema v4 and an explicit v3→v4 migration.** Use two tables: `option_snapshots(snapshot_id, run_id, expiration, fetched_at, payload_json, UNIQUE(run_id, expiration))` and `option_assessments(run_id PRIMARY KEY, submission_hash, result_json, memo_path, completed_at)`, both with run foreign keys. Update `SCHEMA_VERSION`, fresh schema, supported migration versions, and `Capabilities.schema_version` in `tradingagents/plugin/server.py`. Use `BEGIN IMMEDIATE` for assessment acceptance and return the stored result on replay; do not replace stored quote payloads.

```sql
CREATE TABLE option_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  expiration TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE (run_id, expiration)
);
```

- [ ] **Step 4: Update the existing capability test from schema version 3 to 4, run `pytest tests/test_plugin_store.py tests/test_plugin_capabilities.py -q`, then commit only store/server/test files for this task.** Include migration of a populated v3 database; no existing run row may be changed.

### Task 3: Expose guarded option-chain MCP tools

**Files:** Modify `tradingagents/plugin/data.py`, `tradingagents/plugin/server.py`, `tests/test_plugin_data.py`, `tests/test_plugin_capabilities.py`, and `tests/test_plugin_runtime.py`.

**Interfaces:**
- Produces `PluginTools.list_option_expirations(run_id: str) -> dict`, `PluginTools.get_option_chain(run_id: str, expiration: str, offset: int = 0, limit: int = 100) -> dict`, and `PluginTools.get_option_assessment(run_id: str) -> dict | None`.
- `get_option_chain` returns `snapshot_id`, `fetched_at`, `expiration`, `underlying`, one contract page, `next_offset` or `None`, and `complete`. A repeated read uses the saved snapshot without a second provider request.

- [ ] **Step 1: Write failing tool tests.** Patch `fetch_expirations`/`fetch_chain` with fixtures; assert a completed same-day stock run returns expirations and a saved chain page. Verify `offset=0, limit=1` gives `next_offset=1` and the second call returns the remaining row without refetch. Reject `offset<0`, `limit` outside `1..100`, wrong/expired dates, crypto, and a run from the previous day. A provider failure returns an explicit unavailable result with no snapshot.

```python
first = tools.get_option_chain(run_id, "2026-10-16", limit=1)
second = tools.get_option_chain(run_id, "2026-10-16", offset=first["next_offset"], limit=1)
assert first["snapshot_id"] == second["snapshot_id"]
assert second["complete"] is True
```

- [ ] **Step 2: Run red.** `pytest tests/test_plugin_data.py -q` must fail on missing methods.
- [ ] **Step 3: Implement `_require_current_completed_stock_run(run_id)` once and call it from live retrieval and submission methods.** Compare `normalized_inputs["analysis_date"]` with existing `get_current_date()`, check `status == "completed"`, `asset_type == "stock"`, and `_require_compatible_run(run)`. Use the stored canonical symbol, never an LLM-provided ticker. Reject exchange-suffixed/non-US symbols, invalid or expired expirations, and non-USD chains. Check `list_option_snapshots(run_id)` before calling the provider so a repeated expiration read uses saved data. `get_option_assessment` remains read-only and can return an older saved receipt without fetching quotes. Add the methods to `public_operations()` and `get_capabilities().available_tools`; do not change the ordinary analyst stage permissions.

```python
def _require_current_completed_stock_run(self, run_id: str) -> RunRecord:
    run = self._store.get_run(run_id)
    _require_compatible_run(run)
    if run.status != "completed" or run.normalized_inputs["asset_type"] != "stock":
        raise ValueError("options require a completed stock analysis")
    if run.normalized_inputs["analysis_date"] != get_current_date():
        raise ValueError("options require a current-date stock analysis")
    return run
```

- [ ] **Step 4: Add the three new names to the exact tool sets in `tests/test_plugin_capabilities.py` and `tests/test_plugin_runtime.py`; run `pytest tests/test_plugin_data.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q`, then commit this tool slice.** Confirm MCP tool discovery sees names and bounded argument schemas.

### Task 4: Validate selections, calculate economics, and save the memo

**Files:** Extend `tradingagents/plugin/options.py`, `tradingagents/plugin/data.py`, `tradingagents/plugin/server.py`, `tests/test_plugin_options.py`, `tests/test_plugin_data.py`, `tests/test_plugin_capabilities.py`, and `tests/test_plugin_runtime.py`.

**Interfaces:**
- Produces `evaluate_assessment(run_id: str, ticker: str, thesis: str, candidates: list[dict], preferred: str | None, no_trade_reason: str | None, snapshots: list[dict], verified_close: float | None) -> dict` and `PluginTools.submit_option_assessment(run_id: str, thesis: str, candidates: list[dict], preferred: str | None = None, no_trade_reason: str | None = None) -> dict`.
- Each candidate has `strategy` (`cash_secured_put`/`long_call`), `snapshot_id`, `contract_symbol`, `rationale`, `expiry_rationale`, and `failure_condition`. At most one per strategy. `preferred` must name one submitted strategy or be `None`; zero candidates require `no_trade_reason`.

- [ ] **Step 1: Write failing pure tests for selection and arithmetic.** Use fixture snapshots with `AAPL261016P00150000` at strike 150, bid 2.00, ask 2.20 and `AAPL261016C00160000` at strike 160, bid 3.80, ask 4.00. Assert put collateral 15000, credit 200, breakeven 148, maximum loss 14800; assert call debit/max loss 400 and breakeven 164. Reject an adjusted root (`AAPL1...`), `contract_size != "REGULAR"`, currency other than USD, missing/crossed/zero/nonfinite quotes, absent symbol, wrong side, an expired contract, duplicate strategy, or preferred strategy absent from candidates. Assert warnings for a spread over 20% of midpoint, zero/unknown volume and open interest, a last trade over seven days old, and a provider spot more than 5% from an available verified close. A no-trade memo must contain the reason and no invented contract.

```python
put_choice = {"strategy": "cash_secured_put", "snapshot_id": "snapshot-1",
              "contract_symbol": "AAPL261016P00150000", "rationale": "Entry below support",
              "expiry_rationale": "Allows thesis time", "failure_condition": "Support breaks"}
snapshot = {"snapshot_id": "snapshot-1", "run_id": run_id,
            "expiration": "2026-10-16", "fetched_at": "2026-09-27T14:00:00Z",
            "payload": {"contracts": [{"contract_symbol": "AAPL261016P00150000",
                                       "side": "put", "strike": 150, "bid": 2.0,
                                       "ask": 2.2, "currency": "USD",
                                       "contract_size": "REGULAR"}]}}
assessment = evaluate_assessment(run_id, "AAPL", "Bullish above support", [put_choice],
                                 "cash_secured_put", None, [snapshot], verified_close=None)
assert assessment["candidates"][0]["collateral"] == 15000
assert assessment["candidates"][0]["breakeven"] == 148
```

- [ ] **Step 2: Run red.** `pytest tests/test_plugin_options.py -q` must fail on missing evaluator.
- [ ] **Step 3: Implement Pydantic shape checks and deterministic validation/math in `options.py`.** Parse OCC-style contract IDs, require exact root/side/expiry/strike agreement with the saved row and run ticker, `contract_size == "REGULAR"`, `currency == "USD"`, finite positive bid/ask and `bid <= ask`. Calculate with `Decimal(str(value))` and serialize two-decimal dollar results. Keep source fetch time, last-trade time, spread, volume/open interest, and the linked stock report path in the result. Warn when spread exceeds 20% of the bid/ask midpoint, volume or open interest is zero/unknown, or last trade is more than seven calendar days before fetch time; these describe liquidity/activity, not quote age. Compare provider underlying price to a machine-readable verified stock close if available and flag a difference above 5%; otherwise say that comparison is unavailable. Do not infer probability of profit.

```python
put_credit = Decimal(str(row["bid"])) * 100
put_collateral = Decimal(str(row["strike"])) * 100
put_max_loss = put_collateral - put_credit
call_max_loss = Decimal(str(row["ask"])) * 100
```

- [ ] **Step 4: Write failing end-to-end submit/replay tests.** Assert the result is saved in SQLite, `options_memo.md` is concise, and an identical replay repairs a deliberately removed memo file without refetching or changing the receipt. A conflicting payload must leave the stored receipt and memo untouched. Patch the atomic writer to raise once; retry must recover from the committed receipt. Ensure `get_option_assessment` returns the accepted result after reopening the store.
- [ ] **Step 5: Implement submission and atomic file writing.** Check the run guard, load only same-run snapshots named by candidates, read the verified market snapshot's close from stored evidence when its format allows (otherwise pass `None`), evaluate, call `save_option_assessment` in a transaction, then write the memo from the stored receipt via a temporary file in the run report directory and `os.replace`. On replay, use the stored memo text. Never write to a caller-provided path.
- [ ] **Step 6: Advertise `submit_option_assessment` in capabilities and the exact tool sets, run `pytest tests/test_plugin_options.py tests/test_plugin_data.py tests/test_plugin_store.py tests/test_plugin_capabilities.py tests/test_plugin_runtime.py -q`, then commit only task files.**

### Task 5: Package the dedicated skill and verify product behavior

**Files:** Create `plugins/tradingagents/skills/options-trading/SKILL.md`; modify `plugins/tradingagents/README.md`, `docs/plugin-runtime.md`, `tests/test_plugin_package.py`, `tests/test_plugin_runtime.py`, and `scripts/smoke_plugin_protocol.py` if its expected tool list is fixed.

**Interfaces:**
- Skill description triggers on a request for a specific options trade, cash-secured put, long call, or options comparison; stock-only requests remain in `trading-analysis`. The skill reads the sibling `../trading-analysis/SKILL.md` for a new or incomplete stock run, then uses `list_option_expirations`, `get_option_chain`, `submit_option_assessment`, and `get_option_assessment`.

- [ ] **Step 1: Write failing skill-discovery and protocol tests.** Assert the manifest still points to `./skills/`, both `SKILL.md` files exist, the new description names options intent and does not claim ordinary stock analysis, all four options tools are discovered, and no new tool exposes order placement. Add one fixture-backed MCP call for `get_option_assessment` after submission and one for unavailable chains.

```python
skill = (ROOT / "skills/options-trading/SKILL.md").read_text()
assert skill.startswith("---\nname: options-trading\n")
assert "cash-secured put" in skill and "long call" in skill
```

- [ ] **Step 2: Run red.** `pytest tests/test_plugin_package.py tests/test_plugin_runtime.py -q` must fail on absent skill or tool discovery.
- [ ] **Step 3: Write the short skill.** Its procedure: detect options intent; complete or resume the normal same-day stock run using `trading-analysis`; for each ticker read expirations, inspect all pages of chosen chains, assess both requested strategies without forcing a trade, submit selected contract identifiers and rationale, then show the saved memo plus a compact cross-ticker comparison. On source failure or unsupported ticker, report `no trade/unavailable` and the run ID. Keep automatic invocation enabled. Document explicit `$options-trading`, plugin refresh/restart, current-quote limitations, and the full-report link.

```markdown
---
name: options-trading
description: Assess stock tickers for cash-secured puts or long calls using TradingAgents research and current option chains; use for specific option trade suggestions or comparisons.
---

# Options trading

For a new or unfinished stock run, read and follow the sibling
`../trading-analysis/SKILL.md` through `finalize_analysis`. Reuse an already
completed same-day run when supplied. For each ticker, call
`list_option_expirations`, page through `get_option_chain` for the expirations
you choose, and submit at most one cash-secured put and one long call via
`submit_option_assessment`. Use `get_option_assessment` after an uncertain
submission or when resuming. Never invent a contract or premium. A no-trade
result is valid. Show the short saved memo and link the full stock report;
for several tickers, add a compact comparison. Never place an order.
```
- [ ] **Step 4: Validate the skill with `quick_validate.py` from the bundled skill-creator tools.** Exercise representative direct, indirect, stock-only, and unavailable-data prompts in a local Codex host if available; record observed activation and adjust only the description or instructions that misroute. Static tests alone cannot prove model-driven activation. No live brokerage action.
- [ ] **Step 5: Run `pytest tests/test_plugin_package.py tests/test_plugin_runtime.py -q`, `python scripts/smoke_plugin_protocol.py`, full `pytest -q`, and `ruff check .`.** If a live Codex host is available, reinstall the local plugin and verify one fixture-backed full run, quote paging, memo, and interrupted-run recovery; record host version, date, and result in `docs/` without claiming it when unavailable. Commit task files only after checks pass.

## Completion check

Confirm every spec section has an owning task: skill routing (5), quote source and pages (1/3), run and historical boundaries (3), durable snapshot and replay (2/4), contract and payoff checks (4), concise memo and multi-ticker orchestration (4/5), and plugin/report regression (5). Review the final diff to ensure no unrelated README or `.superpowers/brainstorm` files entered commits.
