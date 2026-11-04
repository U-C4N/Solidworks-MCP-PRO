"""Read-only identity, tool-profile and client-configuration diagnostics.

This module never starts a configured command, imports the native CAD backend,
or attaches to SOLIDWORKS. A server can supply its actual published catalogue;
an offline invocation explicitly leaves that connection check unverified.
Only an allowlisted projection of client settings is returned, never raw args,
environment values, parser exception text, or unrelated MCP configuration.
"""
from __future__ import annotations

import argparse
import ast
from importlib import metadata
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import tomllib
from typing import Iterable

from . import __version__


MAX_CONFIG_BYTES = 2 * 1024 * 1024
_PACKAGES = ("solidworks-mcp-pro", "solidworks-mcp", "mcp", "pywin32", "comtypes", "Pillow")
_MODULES = {"solidworks_mcp_pro", "solidworks_mcp", "solidworks_mcp.server"}
_PRO_LAUNCHERS = {"solidworks-mcp-pro", "solidworks-mcp-pro.exe"}
_NATIVE_LAUNCHERS = {"solidworks-mcp", "solidworks-mcp.exe"}
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,255}\Z")
_SECRET = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|bearer\s+\S+)")
_PYTHON = re.compile(r"python(?:w|\d+(?:\.\d+)?)?(?:\.exe)?\Z", re.I)


def _issue(code: str, message: str, *, severity: str = "error", **fields) -> dict:
    return {"code": code, "severity": severity, "message": message, **fields}


def _safe_path(value) -> str | None:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 for char in value) or _SECRET.search(value)
            or "://" in value):
        return None
    return value


def _basename(value: str) -> str:
    return PureWindowsPath(value).name if "\\" in value else Path(value).name


def _absolute(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _same_path(first: str, second: str) -> bool:
    if PureWindowsPath(first).drive or PureWindowsPath(second).drive:
        return str(PureWindowsPath(first)).casefold() == str(PureWindowsPath(second)).casefold()
    return Path(first).expanduser().resolve() == Path(second).expanduser().resolve()


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("FILE_TOO_LARGE")
    return path.read_text(encoding="utf-8-sig")


def _source_version(path: Path) -> str | None:
    """Read a version literal without executing an arbitrary source file."""
    try:
        tree = ast.parse(_read_text(path))
        for item in tree.body:
            if isinstance(item, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__" for target in item.targets
            ):
                value = ast.literal_eval(item.value)
                return value if isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z.+_-]{1,80}", value) else None
    except (OSError, ValueError, SyntaxError, TypeError):
        pass
    return None


def _compact_names() -> set[str] | None:
    """Read the local compact declaration without importing MCP or COM."""
    try:
        tree = ast.parse(_read_text(Path(__file__).with_name("fast_tools.py")))
        for item in tree.body:
            if not isinstance(item, ast.Assign) or not any(
                isinstance(target, ast.Name) and target.id == "COMPACT_TOOLS" for target in item.targets
            ):
                continue
            value = item.value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id == "frozenset" and len(value.args) == 1 and not value.keywords):
                value = value.args[0]
            names = ast.literal_eval(value)
            if isinstance(names, (list, tuple, set, frozenset)) and all(
                isinstance(name, str) and _TOOL_NAME.fullmatch(name) for name in names
            ):
                return set(names)
    except (OSError, ValueError, SyntaxError, TypeError):
        pass
    return None


def runtime_identity() -> dict:
    """Describe this Python process, not another client's running MCP process."""
    versions = {}
    for package in _PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "package_version": __version__,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "python_executable": sys.executable,
        "package_import_path": str(Path(__file__).resolve().parent),
        "package_versions": versions,
    }


def _tool_names(values: Iterable | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes, dict)):
        raise ValueError("Tool catalogues must be lists of names or tool descriptors")
    names = []
    for value in values:
        name = value if isinstance(value, str) else value.get("name") if isinstance(value, dict) else getattr(value, "name", None)
        if not isinstance(name, str):
            raise ValueError("Tool descriptors must contain a name")
        if name.startswith("mcp__"):
            name = name.rsplit("__", 1)[-1]
        if not _TOOL_NAME.fullmatch(name):
            raise ValueError("Invalid tool name in catalogue")
        names.append(name)
    return names


def compare_tool_catalogue(
    tool_profile: str,
    published_tools: Iterable | None = None,
    *,
    native_tools: Iterable | None = None,
    pro_tools: Iterable | None = None,
    compact_tools: Iterable | None = None,
) -> dict:
    """Compare supplied observations; never discover a server by launching it."""
    if tool_profile not in {"full", "compact"}:
        raise ValueError("tool_profile must be full or compact")
    published = _tool_names(published_tools)
    native, pro = _tool_names(native_tools), _tool_names(pro_tools)
    expected = (_tool_names(compact_tools) if compact_tools is not None else _compact_names()) if tool_profile == "compact" else (
        native + pro if native is not None and pro is not None else None
    )
    result = {
        "tool_profile": tool_profile,
        "observation_supplied": published is not None,
        "expected_count": len(set(expected)) if expected is not None else None,
        "published_count": len(published) if published is not None else None,
        "comparison": "unverified",
        "missing_tools": [], "unexpected_tools": [], "duplicate_tools": [],
    }
    if published is not None:
        result["duplicate_tools"] = sorted(name for name in set(published) if published.count(name) > 1)
        result["contains_pro_tools"] = any(name.startswith("pro_") for name in published)
    if published is not None and expected is not None:
        result["missing_tools"] = sorted(set(expected) - set(published))
        result["unexpected_tools"] = sorted(set(published) - set(expected))
        result["comparison"] = "match" if not any(result[key] for key in (
            "missing_tools", "unexpected_tools", "duplicate_tools"
        )) else "mismatch"
    return result


def _launch_settings(settings: dict) -> dict:
    """Project only recognisable launcher identity and safe, known options."""
    command = _safe_path(settings.get("command"))
    filename = _basename(command).lower() if command else ""
    kind = "python" if _PYTHON.fullmatch(filename) else "pro_launcher" if filename in _PRO_LAUNCHERS else (
        "native_launcher" if filename in _NATIVE_LAUNCHERS else "unrecognised"
    )
    args = settings.get("args", [])
    args_valid = isinstance(args, list) and all(isinstance(value, str) for value in args)
    module, profile, profile_explicit = None, "full", False
    invalid_profile = False
    server_args = args if args_valid and kind in {"pro_launcher", "native_launcher"} else []
    if args_valid and kind == "python" and "-m" in args:
        index = args.index("-m")
        python_flags = {"-u", "-B", "-I", "-E", "-s", "-S", "-O", "-OO", "-q"}
        if all(value in python_flags for value in args[:index]) and index + 1 < len(args):
            if args[index + 1] in _MODULES:
                module = args[index + 1]
                server_args = args[index + 2:]
    if server_args:
        for index, value in enumerate(server_args):
            if value == "--tool-profile" or value.startswith("--tool-profile="):
                profile_explicit = True
                candidate = value.partition("=")[2] if "=" in value else server_args[index + 1] if index + 1 < len(server_args) else None
                if candidate not in {"full", "compact"}:
                    invalid_profile = True
                else:
                    profile = candidate
    recognised = kind in {"pro_launcher", "native_launcher"} or module is not None
    # Do not expose an unknown command even if its name looks like a path.
    result = {
        "launcher_kind": kind,
        "command": command if recognised and kind != "unrecognised" else None,
        "module": module,
        "tool_profile": profile if not invalid_profile else None,
        "profile_explicit": profile_explicit,
        "valid_arguments": args_valid,
        "invalid_profile": invalid_profile,
        "one_shot_command": any(value in {"--doctor", "--connection-doctor", "--version", "--help", "-h"} for value in server_args),
        "recognised_solidworks_launcher": recognised,
        "environment_values_redacted": len(settings.get("env", {})) if isinstance(settings.get("env"), dict) else 0,
        "enabled": settings.get("enabled") is not False and settings.get("disabled") is not True,
    }
    environment = settings.get("env")
    if isinstance(environment, dict):
        workspace = _safe_path(environment.get("SW_MCP_PRO_WORKSPACE"))
        if workspace is not None:
            result["workspace"] = workspace
            result["environment_values_redacted"] -= 1
    cwd = _safe_path(settings.get("cwd"))
    if cwd is not None:
        result["cwd"] = cwd
    return result


def inspect_client_config(
    path: Path | str,
    *,
    expected_python: str | None = None,
    expected_profile: str | None = None,
) -> dict:
    """Read TOML/JSON MCP sections without returning their raw contents."""
    path = Path(path).expanduser()
    report = {"path": str(path), "read_only": True, "servers": [], "issues": []}
    try:
        content = _read_text(path)
        data = tomllib.loads(content) if path.suffix.lower() == ".toml" else json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("INVALID_ROOT")
        servers = data.get("mcp_servers", data.get("mcpServers", {}))
        if not isinstance(servers, dict):
            raise ValueError("INVALID_SERVERS")
    except (OSError, ValueError, UnicodeError) as exc:
        report["status"] = "unreadable"
        # Parser error messages can quote secrets from the original document.
        report["issues"].append(_issue("CLIENT_CONFIG_UNREADABLE", "Client configuration could not be read or parsed.",
                                       error_type=type(exc).__name__))
        return report
    for name, settings in servers.items():
        if not isinstance(settings, dict):
            continue
        launch = _launch_settings(settings)
        related_name = isinstance(name, str) and re.search(r"solid[ _-]?works", name, re.I)
        if not related_name and not launch["recognised_solidworks_launcher"]:
            continue
        # A nonstandard server key can contain arbitrary user text; omit it.
        launch["name"] = name if isinstance(name, str) and _TOOL_NAME.fullmatch(name) and not _SECRET.search(name) else "[redacted]"
        issues = []
        if not launch["recognised_solidworks_launcher"]:
            issues.append(_issue("CLIENT_LAUNCHER_UNKNOWN", "SolidWorks entry uses an unrecognised launcher; it was not executed."))
        elif launch["module"] in {"solidworks_mcp", "solidworks_mcp.server"} or launch["launcher_kind"] == "native_launcher":
            issues.append(_issue("CLIENT_TARGETS_NATIVE_SERVER", "Client launches the native MCP rather than SolidWorks MCP PRO."))
        if not launch["valid_arguments"] or launch["invalid_profile"]:
            issues.append(_issue("CLIENT_ARGUMENTS_INVALID", "Client arguments or tool profile are invalid."))
        if launch["one_shot_command"]:
            issues.append(_issue("CLIENT_ONE_SHOT_COMMAND", "Client invokes a one-shot command rather than starting the MCP server."))
        if expected_profile is not None and launch["tool_profile"] != expected_profile:
            issues.append(_issue("CLIENT_PROFILE_MISMATCH", "Configured tool profile differs from the expected profile.",
                                 expected=expected_profile, found=launch["tool_profile"]))
        command = launch["command"]
        if launch["launcher_kind"] == "python" and command:
            if not _absolute(command):
                issues.append(_issue("CLIENT_PYTHON_UNRESOLVED", "Relative Python command depends on the client's environment.", severity="warning"))
            elif expected_python and not _same_path(command, expected_python):
                issues.append(_issue("CLIENT_PYTHON_MISMATCH", "Client Python executable differs from this process."))
            elif os.name == "nt" or not PureWindowsPath(command).drive:
                if not Path(command).is_file():
                    issues.append(_issue("CLIENT_EXECUTABLE_MISSING", "Configured Python executable is missing."))
        if not launch["enabled"]:
            issues.append(_issue("CLIENT_SERVER_DISABLED", "Client entry is disabled.", severity="warning"))
        launch["issues"] = issues
        report["servers"].append(launch)
    if not report["servers"]:
        report["issues"].append(_issue("CLIENT_SERVER_NOT_FOUND", "No recognisable SolidWorks MCP entry found.", severity="warning"))
    report["status"] = "mismatch" if any(
        issue["severity"] == "error" for issue in report["issues"] + [issue for item in report["servers"] for issue in item["issues"]]
    ) else "checked"
    return report


def default_client_config_paths(source_root: Path | None = None) -> list[Path]:
    """Return existing conventional files only; do not scan arbitrary directories."""
    codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    paths = [codex / "config.toml", Path.home() / ".claude.json"]
    if os.environ.get("APPDATA"):
        paths.append(Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json")
    if source_root is not None:
        paths.append(Path(source_root) / ".mcp.json")
    return [path for path in paths if path.is_file()]


def build_connection_report(
    *,
    source_root: Path | str | None = None,
    tool_profile: str = "full",
    published_tools: Iterable | None = None,
    native_tools: Iterable | None = None,
    pro_tools: Iterable | None = None,
    compact_tools: Iterable | None = None,
    client_configs: Iterable[Path | str] = (),
    expected_client_profile: str | None = None,
) -> dict:
    """Build a bounded diagnostic; callers supply actual live catalogue evidence."""
    if expected_client_profile is not None and expected_client_profile not in {"full", "compact"}:
        raise ValueError("expected_client_profile must be full or compact")
    runtime = runtime_identity()
    catalogue = compare_tool_catalogue(tool_profile, published_tools, native_tools=native_tools,
                                       pro_tools=pro_tools, compact_tools=compact_tools)
    issues = []
    source = {"checked": False}
    inferred = Path(__file__).resolve().parents[2]
    root = Path(source_root).expanduser().resolve() if source_root is not None else inferred if (inferred / "pyproject.toml").is_file() else None
    if root is not None:
        source = {"checked": True, "root": str(root), "project_version": None,
                  "module_version": _source_version(root / "src" / "solidworks_mcp_pro" / "__init__.py")}
        try:
            project = tomllib.loads(_read_text(root / "pyproject.toml"))
            version = project.get("project", {}).get("version")
            if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+_-]{1,80}", version):
                source["project_version"] = version
        except (OSError, ValueError, UnicodeError, AttributeError):
            pass
        for field in ("project_version", "module_version"):
            if source[field] is None:
                issues.append(_issue("SOURCE_VERSION_UNREADABLE", "Source version could not be read safely.", field=field, severity="warning"))
            elif source[field] != runtime["package_version"]:
                issues.append(_issue("SOURCE_VERSION_MISMATCH", "Source version differs from the imported package.",
                                     field=field, expected=source[field], found=runtime["package_version"]))
        expected_import = root / "src" / "solidworks_mcp_pro"
        if expected_import.is_dir() and not _same_path(str(expected_import), runtime["package_import_path"]):
            issues.append(_issue("SOURCE_IMPORT_PATH_MISMATCH", "This Python process imported another package location."))
    installed = runtime["package_versions"].get("solidworks-mcp-pro")
    if installed is None:
        issues.append(_issue("PACKAGE_METADATA_MISSING", "PRO distribution metadata is unavailable; an import alone does not prove installation.", severity="warning"))
    elif installed != runtime["package_version"]:
        issues.append(_issue("INSTALLED_VERSION_MISMATCH", "Installed distribution version differs from the imported package.",
                             expected=runtime["package_version"], found=installed))
    if catalogue["comparison"] == "mismatch":
        issues.append(_issue("TOOL_PROFILE_MISMATCH", "Published tool catalogue differs from the expected profile."))
    elif catalogue["comparison"] == "unverified":
        issues.append(_issue("LIVE_CATALOGUE_UNVERIFIED", "No complete live catalogue comparison was supplied; this report does not verify another client's connection.", severity="warning"))
    if catalogue.get("contains_pro_tools") is False:
        issues.append(_issue("PRO_TOOLS_NOT_PUBLISHED", "Observed catalogue exposes no PRO tools; it may belong to a different MCP server."))
    clients = [inspect_client_config(path, expected_python=runtime["python_executable"],
                                     expected_profile=expected_client_profile) for path in client_configs]
    for client in clients:
        for issue in client["issues"] + [issue for item in client["servers"] for issue in item["issues"]]:
            issues.append({**issue, "client_config": client["path"]})
    has_errors = any(issue["severity"] == "error" for issue in issues)
    incomplete = catalogue["comparison"] != "match" or any(issue["severity"] == "warning" for issue in issues)
    status = "mismatch" if has_errors else "incomplete" if incomplete else "ready"
    return {
        "schema_version": 1, "ok": not has_errors, "healthy": status == "ready", "status": status,
        "read_only": True, "cad_checked": False, "configured_commands_executed": False,
        "scope": "Current Python process, supplied tool observations and explicitly selected client files; no CAD connection test.",
        "runtime": runtime, "source": source, "tools": catalogue, "client_configs": clients, "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--tool-profile", choices=("full", "compact"), default="full")
    parser.add_argument("--expected-client-profile", choices=("full", "compact"))
    parser.add_argument("--client-config", type=Path, action="append", default=[])
    parser.add_argument("--include-default-clients", action="store_true", help="Inspect existing conventional Codex/Claude config files")
    parser.add_argument("--tool-catalogue", type=Path, help="Read a JSON observation with published_tools, and optionally native_tools/pro_tools")
    args = parser.parse_args(argv)
    catalogues = {}
    if args.tool_catalogue is not None:
        try:
            value = json.loads(_read_text(args.tool_catalogue))
            if not isinstance(value, dict):
                raise ValueError("INVALID_CATALOGUE")
            catalogues = {key: value[key] for key in ("published_tools", "native_tools", "pro_tools") if key in value}
        except (OSError, ValueError, UnicodeError):
            print(json.dumps({"ok": False, "status": "invalid_input", "code": "TOOL_CATALOGUE_UNREADABLE"}))
            return 2
    configs = list(args.client_config)
    if args.include_default_clients:
        configs.extend(path for path in default_client_config_paths(args.source_root) if path not in configs)
    try:
        report = build_connection_report(source_root=args.source_root, tool_profile=args.tool_profile,
            expected_client_profile=args.expected_client_profile, client_configs=configs, **catalogues)
    except (ValueError, TypeError, OSError):
        print(json.dumps({"ok": False, "status": "invalid_input", "code": "CONNECTION_DOCTOR_INPUT_INVALID"}))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False))
    return 1 if report["status"] == "mismatch" else 2 if report["status"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
