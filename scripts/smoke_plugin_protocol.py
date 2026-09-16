"""Check the installed MCP plugin over stdio without pytest or source imports."""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "get_capabilities",
    "start_analysis",
    "get_analysis",
    "get_stock_data",
    "get_indicators",
    "get_verified_market_snapshot",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_global_news",
    "get_insider_transactions",
    "get_macro_indicators",
    "get_prediction_markets",
    "resolve_instrument_identity",
    "fetch_stocktwits_messages",
    "fetch_reddit_posts",
}


def assert_installed_import(directory: Path) -> None:
    code = """\
import site
from pathlib import Path
import tradingagents

origin = Path(tradingagents.__file__).resolve()
roots = [Path(value).resolve() for value in site.getsitepackages()]
assert any(origin.is_relative_to(root) for root in roots), origin
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        details = exc.stderr or exc.stdout or "no output"
        raise AssertionError(f"installed import timed out after 30 seconds: {details}") from exc
    assert result.returncode == 0, result.stderr


async def check() -> None:
    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        assert_installed_import(directory_path)
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
        env["TRADINGAGENTS_TEMPERATURE"] = "invalid-for-api"
        params = StdioServerParameters(
            command=str(Path(sys.executable).with_name("tradingagents-mcp")),
            args=["--state-dir", str(directory_path / "state")],
            cwd=directory,
            env=env,
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
            result = await session.call_tool("get_capabilities", {})
            assert not result.isError
            assert set(result.structuredContent["available_tools"]) == EXPECTED_TOOLS
            assert result.structuredContent["data_sources"]["fred"]["credential_present"] is False


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), timeout=30))
