import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "configure_clients.py"
spec = importlib.util.spec_from_file_location("configure_clients", SCRIPT)
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


@pytest.mark.parametrize("root", [
    'C:\\Users\\Çağrı User\\CAD "Özel"\\Solidworks-MCP-PRO',
    '/home/çalışma alanı/solidworks 🔧',
    "C:\\tabs\tand\nnewlines\\del\x7f",
])
def test_config_round_trip_escaping(root):
    python = root + "/.venv/Scripts/python.exe"
    workspace = root + "/artifacts"
    contents = configure.render_configurations(python, workspace, root)
    codex = tomllib.loads(contents["codex.toml"])["mcp_servers"]["solidworks-pro"]
    assert codex["command"] == python
    assert codex["args"] == ["-m", "solidworks_mcp_pro", "--tool-profile", "compact"]
    assert codex["cwd"] == root
    assert codex["env"]["SW_MCP_PRO_WORKSPACE"] == workspace
    for filename in ["claude-desktop.json", "claude-code.json"]:
        server = json.loads(contents[filename])["mcpServers"]["solidworks-pro"]
        assert server["command"] == python
        assert server["args"] == codex["args"]
        assert server["env"] == codex["env"]
    assert json.loads(contents["claude-code.json"])["mcpServers"]["solidworks-pro"]["type"] == "stdio"


def test_surrogates_are_rejected():
    with pytest.raises(ValueError, match="surrogates"):
        configure.toml_string("bad\ud800path")


def test_generated_files_are_local_and_absolute(tmp_path):
    root = tmp_path / "Çağrı CAD project"
    root.mkdir()
    sentinel = root / "claude_desktop_config.json"
    sentinel.write_text('{"do-not-edit": true}', encoding="utf-8")
    generated = configure.generate_configs(root)
    assert set(generated) == {"codex.toml", "claude-desktop.json", "claude-code.json"}
    assert all(path.parent == root / "artifacts" / "configs" for path in generated.values())
    contents = json.loads(generated["claude-code.json"].read_text(encoding="utf-8"))
    server = contents["mcpServers"]["solidworks-pro"]
    expected_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert server["command"] == str(expected_python)
    assert Path(server["command"]).is_absolute()
    assert server["env"]["SW_MCP_PRO_WORKSPACE"] == str(root / "artifacts")
    assert sentinel.read_text(encoding="utf-8") == '{"do-not-edit": true}'
    assert not list((root / "artifacts" / "configs").glob(".swpro-config-*"))


def test_cli_custom_output_and_idempotence(tmp_path):
    output = tmp_path / "generated snippets ü 🔧"
    command = [sys.executable, str(SCRIPT), "--output", str(output)]
    first = subprocess.run(command, capture_output=True, text=True, check=True)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    subprocess.run(command, capture_output=True, text=True, check=True)
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
    assert "settings were not edited" in first.stdout


def test_static_examples_are_valid():
    directory = SCRIPT.parents[1] / "configs"
    tomllib.loads((directory / "codex.example.toml").read_text(encoding="utf-8"))
    for name in ["claude-desktop.example.json", "claude-code.example.json"]:
        assert "mcpServers" in json.loads((directory / name).read_text(encoding="utf-8"))
