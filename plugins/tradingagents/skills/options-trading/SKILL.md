---
name: options-trading
description: Assess stock tickers for cash-secured puts or long calls using TradingAgents research and current option chains; use for specific option trade suggestions or comparisons.
---

# Options trading

Use this skill for a specific options trade request or comparison. Stock-only requests
stay in `trading-analysis`. For a new or unfinished stock run, read and follow the sibling
`../trading-analysis/SKILL.md` through `finalize_analysis`; reuse an already completed
same-day run when supplied. Historical dates and crypto are unsupported.

For each ticker, call `list_option_expirations`, then page through every selected expiration
with `get_option_chain` until `complete` is true. Assess both requested strategies without
forcing a trade. Submit at most one cash-secured put and one long call using the selected
snapshot and contract identifiers, rationale, expiry rationale, and failure condition via
`submit_option_assessment`. After an uncertain submission or when resuming, call
`get_option_assessment`. Each run accepts one assessment; identical submissions replay and
conflicting submissions fail. Start a new same-day run to refresh quotes.

Never invent a contract or premium. Use only verified USD standard 100-share contracts.
Show the quote source and fetch time; last trade time is not quote time. Cash-secured puts
use bid, with collateral `strike × 100`, credit `bid × 100`, breakeven `strike − bid`, and
worst-case loss `(strike − bid) × 100`. Long calls use ask, with debit and maximum loss
`ask × 100` and breakeven `strike + ask`. Figures exclude fees and do not promise fills.
No-trade is valid. On source failure or unsupported tickers, report `no trade/unavailable`
and the run ID. Show the saved short memo and link the full stock report; for multiple
tickers, add a compact comparison. Never place an order.
