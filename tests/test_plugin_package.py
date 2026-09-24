import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "tradingagents"


def test_local_plugin_manifest_and_stdio_contract():
    manifest_path = ROOT / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    mcp_path = ROOT / manifest["mcpServers"]
    servers = json.loads(mcp_path.read_text())["mcpServers"]

    assert manifest["mcpServers"] == "./.mcp.json"
    assert mcp_path.is_file()
    assert servers["tradingagents"]["command"] == "tradingagents-mcp"
    assert not servers["tradingagents"].get("args")


def test_plugin_json_has_no_local_secrets_or_absolute_paths():
    files = [ROOT / ".codex-plugin/plugin.json", ROOT / ".mcp.json"]
    text = "\n".join(path.read_text() for path in files)
    assert "/Users/" not in text and "/home/" not in text
    assert "API_KEY= " not in text and "sk-" not in text


def test_readme_describes_a_personal_marketplace_catalog():
    readme = (ROOT / "README.md").read_text()
    assert "~/.agents/plugins/marketplace.json" in readme
    assert '"path": "./.codex/plugins/tradingagents"' in readme
    assert "Plugins Directory" in readme
