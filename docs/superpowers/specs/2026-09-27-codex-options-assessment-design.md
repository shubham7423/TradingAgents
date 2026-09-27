# Codex options assessment design

Status: proposed for user review (2026-09-27).

## Intent and scope

Extend the local Codex plugin with a dedicated `options-trading` skill. When a user asks to analyze one or more stock tickers for options, it reuses the existing full research workflow, then produces a concise, researched assessment of cash-secured puts and long calls. The user wants specific contracts from an option chain. The agent chooses and explains expiration; no fixed capital limit is assumed. Each candidate shows the cash required or premium at risk. The existing detailed stock report and API-backed CLI remain available.

This is an opt-in follow-on to a completed stock analysis, not a new mandatory stage. For several tickers, the skill completes one analysis and options assessment per ticker, then gives a compact comparison. It may recommend no trade for a ticker or for both strategies. It never places an order.

## Skill routing

Add `plugins/tradingagents/skills/options-trading/SKILL.md` to the existing plugin skills directory. Its name and description target options requests such as “analyze AAPL for cash-secured puts,” “find a long call setup,” and “compare option trades for these tickers.” Codex sees the skill metadata when the plugin is installed and loads its full instructions when a request matches; the user can also invoke `$options-trading` explicitly. Ordinary stock analysis continues to use `trading-analysis` alone. The options skill loads and follows the existing `trading-analysis` skill for the stock research portion instead of copying its stage-by-stage instructions. It then calls the new option-chain and assessment tools. The plugin manifest already points to `./skills/`, so it needs no second plugin or manifest path.

The options skill specifies the required inputs, when to reuse a completed same-day stock run, how to handle missing chain data, and the concise result format. The MCP server supplies quotes and enforces contract and arithmetic checks. Skill activation and results must be tested with direct options requests, indirect requests, ordinary stock requests that should not trigger it, and unavailable-data cases.

## Approach

The chosen approach is a separate assessment linked to an existing completed plugin run. Inserting an options stage into every analysis would complicate the current state schema, checkpoint recovery, and report export for users who only want stocks. A skill-only summary would be smaller but could invent contract details or make unchecked payoff calculations.

The existing `yfinance` dependency supplies current listed expirations and chains. Add named MCP operations to list expirations, retrieve bounded option-chain evidence for a chosen expiration, and submit a structured assessment. The server validates the chosen contract against a saved chain snapshot, computes payoff figures, and writes a short `options_memo.md` next to the run's full report. Keep the source snapshot and fetch time linked to the memo so a resumed Codex session does not silently replace prices. Each completed run gets one options assessment: identical submissions replay its receipt; conflicting submissions fail. A new analysis run is needed for refreshed quotes.

## Workflow

1. On an explicit options request, run or resume the usual research through portfolio decision and `finalize_analysis`. Do not skip the established analyst, debate, trader, or risk steps merely to shorten the final response.
2. Confirm the run is for a stock, is completed, and uses today's local analysis date. Historical stock analyses cannot use today's option chain as though it were historical. The first version supports standard, physically settled, USD equity or ETF options only; report other instruments as unsupported.
3. Read available expirations. The agent chooses a small number of relevant expirations from the thesis and fetches their put/call chains. Each result records source, fetch time, underlying quote if available, contract identifier, expiration, strike, bid, ask, volume, open interest, implied volatility when present, currency, contract size, and last trade time. The last trade time is not described as the quote time. Return bounded pages with explicit continuation until all requested rows are available; never silently truncate a chain.
4. The agent compares cash-secured puts and long calls against the underlying research: direction, price levels, catalysts, downside case, time horizon, option liquidity, and premium. It submits at most one candidate per strategy, marks one as preferred if justified, or records `no_trade` with reasons. A neutral or bearish stock conclusion can yield no trade. Missing or inconsistent chain data cannot be replaced with a model-estimated premium.
5. The server verifies each contract identifier, side, strike, expiration, currency, standard 100-share deliverable, and quote against the saved snapshot. Reject nonfinite or nonpositive prices, crossed quotes, expired contracts, mismatched symbols, adjusted or unverifiable deliverables, and selected contracts absent from that snapshot. No contract passes on `lastPrice` alone. Surface wide spreads, absent volume/open interest, stale last trades, and material quote/underlying mismatches as limitations. If a valid quote is not available, the assessment says no trade or unavailable.
6. Python calculates, per one standard contract and before fees: cash-secured put collateral `strike × 100`, quoted credit `bid × 100`, breakeven `strike − bid`, and worst-case loss `(strike − bid) × 100`; long call debit and maximum loss `ask × 100`, and expiration breakeven `strike + ask`. Use bid for a sale and ask for a purchase; label these as indicative quotes, not guaranteed fills. The memo includes the research thesis, contract terms, expiry rationale, calculation, main failure condition, data gaps, and quote fetch time. The Codex reply shows this memo, not the entire stock report, and links to both artifacts.

## Boundaries and state

The new options skill coordinates the existing run and the new operations. Python owns quote retrieval, validation, calculations, and persistence. It does not construct an LLM client, use Codex credentials, submit a brokerage order, or forecast a probability of profit from implied volatility. Restrict option evidence to a completed run's ticker and a current, compatible analysis date. A quote snapshot has a stable identifier; submitted candidates must reference it. Persist it under the plugin state root using the existing SQLite store, with bounded size and run ownership. Save the memo under the run-owned report directory using an atomic write. Reject a conflicting retry rather than silently changing the quoted basis. The existing stock decision log and reflection math remain stock-oriented; an options suggestion is not recorded as if it were an executed trade or measured option return.

## Verification and acceptance

- A fixture-backed full plugin analysis can be followed by a specific cash-secured put or long call assessment and a short saved memo. A multi-ticker request yields one independent result per ticker and a compact comparison.
- The installed plugin discovers `options-trading`; options requests select it, and ordinary stock requests continue through `trading-analysis`. Explicit `$options-trading` invocation works.
- Tests cover no-trade, unavailable chains, historical-date rejection, wrong ticker/expiration/side, adjusted contracts, crossed or missing bid/ask, idempotent retries, and exact per-contract payoff arithmetic. External data is mocked.
- Existing plugin analysis, report, and reflection tests continue to pass; `ruff check .` passes. A local Codex acceptance check exercises discovery, one completed stock run, chain retrieval, a saved memo, and recovery after interruption.

## Source basis

- [OCC equity option specifications](https://www.theocc.com/clearance-and-settlement/clearing/equity-options-product-specifications): standard equity contracts represent 100 shares; corporate actions can alter deliverables.
- [OIC cash-secured put](https://www.optionseducation.org/strategies/all-strategies/cash-secured-put): assignment obligation, collateral, breakeven, and substantial downside.
- [OIC long call](https://www.optionseducation.org/strategies/all-strategies/long-call): premium at risk, expiration breakeven, and time decay.
- [yfinance option-chain implementation](https://github.com/ranaroussi/yfinance/blob/main/yfinance/ticker.py): available expiration and chain fields in the dependency already used by this repository.
