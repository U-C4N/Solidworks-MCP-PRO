"""Generate reviewable client snippets without editing any client configuration."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

SERVER_NAME = "solidworks-pro"


def toml_string(value: str) -> str:
    """Encode a TOML basic string, including Windows separators and Unicode."""
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("Paths containing unpaired Unicode surrogates cannot be configured")
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def render_configurations(python: str, workspace: str, cwd: str, *, tool_profile: str = "compact") -> dict[str, str]:
    """Render independent Codex, Claude Desktop, and Claude Code snippets."""
    if tool_profile not in {"compact", "full"}:
        raise ValueError("tool_profile must be compact or full")
    arguments = ["-m", "solidworks_mcp_pro", "--tool-profile", tool_profile]
    environment = {"SW_MCP_PRO_WORKSPACE": workspace}
    server = {"command": python, "args": arguments, "env": environment}
    toml = "\n".join([
        "# Generated snippet. Merge this table into your Codex config.toml.",
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {toml_string(python)}",
        "args = [" + ", ".join(toml_string(arg) for arg in arguments) + "]",
        f"cwd = {toml_string(cwd)}",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 180",
        "",
        f"[mcp_servers.{SERVER_NAME}.env]",
        f"SW_MCP_PRO_WORKSPACE = {toml_string(workspace)}",
        "",
    ])
    return {
        "codex.toml": toml,
        "claude-desktop.json": json.dumps({"mcpServers": {SERVER_NAME: server}}, indent=2, ensure_ascii=False) + "\n",
        "claude-code.json": json.dumps({"mcpServers": {SERVER_NAME: {"type": "stdio", **server}}}, indent=2, ensure_ascii=False) + "\n",
    }


def _atomic_write(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".swpro-config-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def generate_configs(root: Path, output: Path | None = None, *, tool_profile: str = "compact") -> dict[str, Path]:
    """Write only the three generated snippet files under output (default artifacts/configs)."""
    root = Path(root).resolve()
    destination = Path(output).resolve() if output is not None else root / "artifacts" / "configs"
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    contents = render_configurations(str(python), str(root / "artifacts"), str(root), tool_profile=tool_profile)
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        _atomic_write(destination / name, content)
    return {name: destination / name for name in contents}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Destination for generated snippets (default: repository/artifacts/configs)")
    parser.add_argument("--tool-profile", choices=("compact", "full"), default="compact")
    args = parser.parse_args(argv)
    result = generate_configs(Path(__file__).resolve().parents[1], args.output, tool_profile=args.tool_profile)
    # Windows redirected consoles can use a legacy code page. Files still use
    # UTF-8; printing a path must not make a successful generation fail.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    for path in result.values():
        print(path)
    print("Generated snippets only; existing Codex and Claude settings were not edited.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
