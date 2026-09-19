import asyncio
import builtins
import contextlib
import json
import os
import selectors
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

EXPECTED_TOOLS = {
    "get_capabilities",
    "start_analysis",
    "get_analysis",
    "submit_stage",
    "list_analyses",
    "cancel_analysis",
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


@pytest.mark.parametrize("extra", [
    {"llm_provider": "openai"},
    {"memory_log_path": "/tmp/unsupported-memory"},
    {"analyst": ["news"]},
])
def test_sdk_rejects_unknown_start_arguments_before_side_effects(tmp_path, monkeypatch, extra):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from tradingagents.plugin import data
    from tradingagents.plugin.server import create_server

    calls = []
    monkeypatch.setattr(data, "resolve_instrument_identity", lambda ticker: calls.append(ticker) or {})
    monkeypatch.setattr(data.TradingMemoryLog, "get_past_context", lambda *args, **kwargs: "")
    server = create_server(tmp_path)
    arguments = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "ticker": "AAPL", "analysis_date": "2026-09-15", **extra,
    }

    async def invoke():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool("start_analysis", arguments)

    result = asyncio.run(invoke())

    assert result.isError
    assert next(iter(extra)) in result.content[0].text
    assert "Extra inputs are not permitted" in result.content[0].text
    assert calls == []
    with sqlite3.connect(tmp_path / "plugin.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0

    arguments.pop(next(iter(extra)))
    arguments.update(analysts=["news"], vendor_overrides={"tools": {"get_news": "yfinance"}})
    accepted = asyncio.run(invoke())
    assert not accepted.isError
    assert accepted.structuredContent["current_stage"] == "analyst/news"
    assert accepted.structuredContent["frozen_config"]["tool_vendors"]["get_news"] == "yfinance"


def test_state_root_is_created_and_probe_removed(tmp_path):
    from tradingagents.plugin.server import prepare_state_root

    root = prepare_state_root(tmp_path / "new")

    assert root == (tmp_path / "new").resolve()
    assert list(root.iterdir()) == []


def test_state_root_rejects_file(tmp_path):
    from tradingagents.plugin.server import prepare_state_root

    root = tmp_path / "file"
    root.write_text("keep", encoding="utf-8")

    with pytest.raises(OSError):
        prepare_state_root(root)

    assert root.read_text(encoding="utf-8") == "keep"


def test_write_probe_is_required(tmp_path, monkeypatch):
    from tradingagents.plugin import server

    def denied(*args, **kwargs):
        raise PermissionError("probe denied")

    monkeypatch.setattr(server.tempfile, "TemporaryFile", denied)

    with pytest.raises(PermissionError, match="probe denied"):
        server.prepare_state_root(tmp_path)


def test_main_rejects_file_state_dir(tmp_path, capsys):
    from tradingagents.plugin.server import main

    root = tmp_path / "file"
    root.write_text("keep", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["--state-dir", str(root)])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    assert "Cannot use plugin state directory" in captured.err
    assert "Set --state-dir to a writable directory." in captured.err


def test_main_hints_install_when_mcp_is_missing(tmp_path, monkeypatch, capsys):
    from tradingagents.plugin import server

    original_import = builtins.__import__

    def missing_mcp(name, *args, **kwargs):
        if name == "mcp.server.fastmcp":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_mcp)

    with pytest.raises(SystemExit) as exc_info:
        server.main(["--state-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    assert "Install the plugin runtime with: python -m pip install 'tradingagents[plugin]'" in captured.err


def test_stdio_protocol_stdout_is_json_rpc(tmp_path):
    pytest.importorskip("mcp")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tradingagents.plugin.server",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    deadline = time.monotonic() + 30
    stdout_lines = []
    stdout_buffer = b""
    stderr_buffer = b""
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    os.set_blocking(stdout_fd, False)
    os.set_blocking(stderr_fd, False)
    selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
    selector.register(stderr_fd, selectors.EVENT_READ, "stderr")

    def send(message):
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def read_available() -> bool:
        nonlocal stdout_buffer, stderr_buffer
        events = selector.select(max(0, deadline - time.monotonic()))
        if not events:
            return False
        for key, _ in events:
            try:
                chunk = os.read(key.fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fd)
            elif key.data == "stdout":
                stdout_buffer += chunk
            else:
                stderr_buffer += chunk
        return True

    def read_line():
        nonlocal stdout_buffer
        while b"\n" not in stdout_buffer:
            assert time.monotonic() < deadline, "MCP response timed out"
            assert read_available(), "MCP response timed out"
            assert time.monotonic() < deadline, "MCP response timed out"
        line, stdout_buffer = stdout_buffer.split(b"\n", 1)
        stdout_lines.append(line.decode() + "\n")

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            }
        )
        read_line()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        read_line()
    finally:
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=max(0, deadline - time.monotonic()))
        while selector.get_map() and time.monotonic() < deadline:
            if not read_available():
                break
        selector.close()
        while b"\n" in stdout_buffer:
            line, stdout_buffer = stdout_buffer.split(b"\n", 1)
            stdout_lines.append(line.decode() + "\n")
        if stdout_buffer:
            stdout_lines.append(stdout_buffer.decode())

    responses = [json.loads(line) for line in stdout_lines]
    assert all(response["jsonrpc"] == "2.0" for response in responses)
    assert [response["id"] for response in responses] == [1, 2]
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == EXPECTED_TOOLS
