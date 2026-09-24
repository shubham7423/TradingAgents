# TradingAgents for Codex

This local MCP plugin runs the installed `tradingagents-mcp` executable. Plugin activation does
not install Python or the TradingAgents package; provision its Python environment separately.

## Install

Use a supported Python version (3.10–3.13); these commands use Python 3.12. From the repository
checkout:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install '.[plugin]'
python -m pip check
command -v tradingagents-mcp
```

For a pinned wheel instead, activate the dedicated environment and install the wheel built from
this checkout:

```bash
python -m pip install './dist/tradingagents-0.4.0-py3-none-any.whl[plugin]'
python -m pip check
command -v tradingagents-mcp
```

The installed Codex host must see `tradingagents-mcp` on its `PATH`. If it does not, edit the
local installed copy of `.mcp.json` and replace `tradingagents-mcp` with the absolute path printed
by `command -v tradingagents-mcp` in that environment.

Set a writable state directory before starting Codex:

```bash
mkdir -p "$HOME/.local/state/tradingagents"
export TRADINGAGENTS_PLUGIN_STATE_DIR="$HOME/.local/state/tradingagents"
```

The optional data credentials are `ALPHA_VANTAGE_API_KEY` and `FRED_API_KEY`. Set only the keys
you have; never put credential values in plugin JSON. Set `TRADINGAGENTS_RESULTS_DIR` to choose
the results root; its default depends on the installed package configuration. `.env` lookup also
depends on the process launch working directory.

## Activate

Add this directory (`plugins/tradingagents`) as a personal local plugin marketplace source in
Codex, then install/enable **tradingagents** from that marketplace. Start a new Codex session
after activation. Verify discovery by calling `get_capabilities`, then make a standalone data
request with an available data source.

Data-source availability varies by provider, credentials, and instrument. Codex subscription
usage is controlled by the host and plugin activation does not grant unlimited model usage.
