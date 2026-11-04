# Connect Codex or Claude

Generated snippets select `--tool-profile compact` (14 workflow tools in v0.6.0).
Existing client settings are not changed. The server itself still defaults to
`full`; generate that variant with `python scripts/configure_clients.py
--tool-profile full`. Compact retains native modelling through `pro_run_plan`
and exact schemas through `pro_tool_help`. See [workflow usage](fast-workflows.md).

The server uses local MCP stdio. Native CAD operations require a licensed, running SOLIDWORKS session on Windows. Offline mesh inspection and previews do not require SOLIDWORKS. Run the server with the same Windows user and elevation level as SOLIDWORKS.

## Install on Windows

From this repository in PowerShell:

```powershell
.\scripts\setup.ps1
```

The script creates or reuses `.venv`, installs this project, and generates three client snippets under `artifacts/configs`. It uses an installed 64-bit Python 3.11+ and prefers Python 3.12. To choose an interpreter or install developer tools:

```powershell
.\scripts\setup.ps1 -Python 'C:\Python312\python.exe' -Dev
```

Production installation uses `pip install .`; `-Dev` uses editable installation with the development dependencies. The script does not download a Python installer or edit global client settings. If PowerShell blocks scripts, run the equivalent commands directly with your installed Python:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe scripts/configure_clients.py
```

The generator can write snippets elsewhere using `--output 'C:\my folder\snippets'`. Regeneration replaces only its three snippet files. Paths containing spaces and Unicode are serialized safely. On non-Windows systems it selects `.venv/bin/python`; COM CAD tools remain Windows-only.

## Codex

Merge the generated `artifacts/configs/codex.toml` table into your Codex `config.toml`, preserving existing tables. Its executable, working directory, and artifact workspace are absolute. Codex supports local stdio servers through the `mcp_servers` table. [Official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

Alternatively, from this checkout in PowerShell, register the server yourself:

```powershell
$ProjectPath = (Get-Location).Path
$ServerPython = Join-Path $ProjectPath '.venv\Scripts\python.exe'
$ArtifactPath = Join-Path $ProjectPath 'artifacts'
codex mcp add solidworks-pro --env "SW_MCP_PRO_WORKSPACE=$ArtifactPath" -- $ServerPython -m solidworks_mcp_pro
```

The CLI command changes Codex's registration when you execute it. Restart/reconnect the client and ask it to call `pro_capabilities` first.

## Claude Code

Merge the generated `artifacts/configs/claude-code.json` server entry into your project `.mcp.json`, preserving existing entries. Or register it from the repository using:

```powershell
$ProjectPath = (Get-Location).Path
$ServerPython = Join-Path $ProjectPath '.venv\Scripts\python.exe'
$ArtifactPath = Join-Path $ProjectPath 'artifacts'
claude mcp add --scope project --env "SW_MCP_PRO_WORKSPACE=$ArtifactPath" --transport stdio solidworks-pro -- $ServerPython -m solidworks_mcp_pro
```

This command edits project registration when you execute it. Open `/mcp` in Claude Code to inspect connection status. The `--` separator keeps server arguments separate from Claude options. [Claude Code MCP reference](https://code.claude.com/docs/en/mcp)

## Claude Desktop

Open **Settings → Developer → Edit Config**. Merge the `solidworks-pro` entry from `artifacts/configs/claude-desktop.json` into the existing `mcpServers` object. On Windows the target file is `%APPDATA%\Claude\claude_desktop_config.json`. Completely quit and restart Claude Desktop after saving. [Official MCP local-server guide](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers)

## Check the connection

Ask the client to discover the server's tools and run `pro_capabilities`. For a CAD task, start SOLIDWORKS and run `get_status` before modeling. Use a separate new document for the first test. A local configuration file being generated does not prove that a client has connected or that a CAD operation has succeeded.

If the client reports a missing executable, rerun setup and regenerate snippets from the current checkout. If tools fail to import, run `.venv\Scripts\python.exe -m solidworks_mcp_pro --help` from the repository to inspect startup options. Logs must go to stderr; stdout is reserved for MCP messages.
