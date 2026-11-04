from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from solidworks_mcp_pro import connection_doctor as doctor


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "source"
    package = root / "src" / "solidworks_mcp_pro"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    executable = tmp_path / "venv" / "python.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"not executed")
    runtime = {
        "package_version": "1.2.3", "python_version": "3.12.0",
        "python_executable": str(executable), "package_import_path": str(package),
        "package_versions": {"solidworks-mcp-pro": "1.2.3", "solidworks-mcp": "0.19.0"},
    }
    monkeypatch.setattr(doctor, "runtime_identity", lambda: deepcopy(runtime))
    return root, runtime


def full_report(environment, **kwargs):
    return doctor.build_connection_report(source_root=environment[0], tool_profile="full",
        published_tools=["new_part", "pro_capabilities"], native_tools=["new_part"],
        pro_tools=["pro_capabilities"], **kwargs)


def codes(report):
    return {item["code"] for item in report["issues"]}


def write_config(tmp_path, server, *, name="solidworks-pro", extra=None):
    path = tmp_path / "client.json"
    servers = {name: server, **(extra or {})}
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def pro_settings(environment, profile="compact"):
    return {"command": environment[1]["python_executable"],
            "args": ["-m", "solidworks_mcp_pro", "--tool-profile", profile]}


def test_matching_running_identity_and_full_catalogue(environment):
    report = full_report(environment)
    assert report["status"] == "ready"
    assert report["ok"] and report["healthy"]
    assert report["read_only"] and not report["cad_checked"]
    assert not report["configured_commands_executed"]
    assert report["tools"]["comparison"] == "match"
    assert report["source"]["module_version"] == "1.2.3"
    assert not report["issues"]


def test_offline_report_does_not_claim_a_live_connection(environment):
    report = doctor.build_connection_report(source_root=environment[0])
    assert report["status"] == "incomplete"
    assert not report["healthy"]
    assert report["tools"]["comparison"] == "unverified"
    assert "LIVE_CATALOGUE_UNVERIFIED" in codes(report)


@pytest.mark.parametrize("field", ["project", "module", "installed"])
def test_package_source_and_distribution_mismatches_are_distinguished(environment, field):
    root, runtime = environment
    if field == "project":
        (root / "pyproject.toml").write_text('[project]\nversion = "9.0"\n', encoding="utf-8")
    elif field == "module":
        (root / "src" / "solidworks_mcp_pro" / "__init__.py").write_text('__version__ = "9.0"', encoding="utf-8")
    else:
        runtime["package_versions"]["solidworks-mcp-pro"] = "9.0"
    report = full_report(environment)
    assert report["status"] == "mismatch" and not report["ok"]
    assert ("INSTALLED_VERSION_MISMATCH" if field == "installed" else "SOURCE_VERSION_MISMATCH") in codes(report)


def test_import_from_another_checkout_is_reported(environment, tmp_path):
    environment[1]["package_import_path"] = str(tmp_path / "another_package")
    assert "SOURCE_IMPORT_PATH_MISMATCH" in codes(full_report(environment))


def test_missing_distribution_metadata_is_not_an_installation_success(environment):
    environment[1]["package_versions"]["solidworks-mcp-pro"] = None
    report = full_report(environment)
    assert report["status"] == "incomplete"
    assert "PACKAGE_METADATA_MISSING" in codes(report)


def test_source_version_is_never_executed(environment, tmp_path):
    marker = tmp_path / "must_not_exist"
    initializer = environment[0] / "src" / "solidworks_mcp_pro" / "__init__.py"
    initializer.write_text(f'__version__ = __import__("pathlib").Path({str(marker)!r}).write_text("bad")', encoding="utf-8")
    report = full_report(environment)
    assert "SOURCE_VERSION_UNREADABLE" in codes(report)
    assert not marker.exists()


def test_compact_catalogue_uses_exact_names_and_understands_client_namespaces():
    report = doctor.compare_tool_catalogue("compact", ["mcp__solidworks__pro_capabilities", "pro_operation"],
        compact_tools=["pro_capabilities", "pro_operation"])
    assert report["comparison"] == "match" and report["expected_count"] == 2


def test_full_catalogue_detects_missing_unexpected_and_duplicate_tools():
    report = doctor.compare_tool_catalogue("full", ["doc_new", "pro_capabilities", "pro_capabilities"],
        native_tools=["new_part"], pro_tools=["pro_capabilities", "pro_render_job"])
    assert report["comparison"] == "mismatch"
    assert report["missing_tools"] == ["new_part", "pro_render_job"]
    assert report["unexpected_tools"] == ["doc_new"]
    assert report["duplicate_tools"] == ["pro_capabilities"]


def test_native_catalogue_does_not_masquerade_as_pro(environment):
    report = doctor.build_connection_report(source_root=environment[0], tool_profile="compact",
        published_tools=["mcp__solidworks__doc_new"], compact_tools=["pro_capabilities"])
    assert report["status"] == "mismatch"
    assert {"TOOL_PROFILE_MISMATCH", "PRO_TOOLS_NOT_PUBLISHED"} <= codes(report)


@pytest.mark.parametrize("catalogue", ["new_part", [{"description": "missing name"}], ["bad\nname"]])
def test_invalid_catalogues_are_rejected_without_reflecting_values(catalogue):
    with pytest.raises(ValueError):
        doctor.compare_tool_catalogue("full", catalogue)


def test_compact_expectations_track_the_local_declaration():
    # Read the declaration independently so future compact additions need no
    # duplicate hardcoded list in either the doctor or this test.
    import ast
    tree = ast.parse(Path(doctor.__file__).with_name("fast_tools.py").read_text(encoding="utf-8"))
    assignment = next(item for item in tree.body if isinstance(item, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "COMPACT_TOOLS" for target in item.targets))
    expected = ast.literal_eval(assignment.value.args[0])
    report = doctor.compare_tool_catalogue("compact", sorted(expected))
    assert report["comparison"] == "match" and report["expected_count"] == len(expected)


def test_json_config_projection_redacts_env_unknown_args_and_other_servers(environment, tmp_path):
    secret = "sk-this-is-a-private-token-123456789"
    settings = pro_settings(environment)
    settings["args"] += ["--unrecognised-token", secret]
    settings["env"] = {"API_KEY": secret, "SW_MCP_PRO_WORKSPACE": str(tmp_path / "outputs")}
    path = write_config(tmp_path, settings, extra={"unrelated": {"command": secret, "env": {secret: secret}}})
    before = path.read_bytes()
    report = doctor.inspect_client_config(path, expected_python=environment[1]["python_executable"], expected_profile="compact")
    assert report["status"] == "checked"
    assert len(report["servers"]) == 1
    result = report["servers"][0]
    assert result["workspace"] == str(tmp_path / "outputs")
    assert result["environment_values_redacted"] == 1
    serialized = json.dumps(report)
    assert secret not in serialized and "API_KEY" not in serialized and "unrecognised-token" not in serialized
    assert path.read_bytes() == before


def test_codex_toml_handles_profile_and_python_mismatches(environment, tmp_path):
    path = tmp_path / "codex.toml"
    other_python = str(tmp_path / "other" / "python.exe")
    path.write_text('[mcp_servers.solidworks-pro]\ncommand = ' + json.dumps(other_python)
        + '\nargs = ["-m", "solidworks_mcp_pro", "--tool-profile", "full"]\n', encoding="utf-8")
    report = doctor.inspect_client_config(path, expected_python=environment[1]["python_executable"], expected_profile="compact")
    found = {issue["code"] for issue in report["servers"][0]["issues"]}
    assert {"CLIENT_PYTHON_MISMATCH", "CLIENT_PROFILE_MISMATCH"} <= found


def test_core_server_and_default_full_profile_are_visible(environment, tmp_path):
    settings = pro_settings(environment)
    settings["args"] = ["-m", "solidworks_mcp"]
    path = write_config(tmp_path, settings)
    report = doctor.inspect_client_config(path, expected_profile="compact")
    server = report["servers"][0]
    assert server["tool_profile"] == "full" and not server["profile_explicit"]
    assert {"CLIENT_TARGETS_NATIVE_SERVER", "CLIENT_PROFILE_MISMATCH"} <= {issue["code"] for issue in server["issues"]}


@pytest.mark.parametrize("arguments", [
    ["-c", "arbitrary secret code", "-m", "solidworks_mcp_pro"],
    ["-m", "another_module", "-m", "solidworks_mcp_pro"],
    ["my_script.py", "-m", "solidworks_mcp_pro"],
])
def test_embedded_module_argument_is_not_a_pro_launcher(environment, tmp_path, arguments):
    settings = pro_settings(environment)
    settings["args"] = arguments
    report = doctor.inspect_client_config(write_config(tmp_path, settings))
    server = report["servers"][0]
    assert not server["recognised_solidworks_launcher"]
    assert "arbitrary secret code" not in json.dumps(report)


def test_unknown_command_is_not_trusted_even_with_pro_module_argument(tmp_path):
    report = doctor.inspect_client_config(write_config(tmp_path, {
        "command": "private-token-command", "args": ["-m", "solidworks_mcp_pro"]}))
    server = report["servers"][0]
    assert not server["recognised_solidworks_launcher"] and server["command"] is None
    assert "private-token-command" not in json.dumps(report)


def test_supported_python_flags_and_equals_profile_are_understood(environment, tmp_path):
    settings = pro_settings(environment)
    settings["args"] = ["-B", "-u", "-m", "solidworks_mcp_pro", "--tool-profile=compact"]
    server = doctor.inspect_client_config(write_config(tmp_path, settings))["servers"][0]
    assert server["recognised_solidworks_launcher"] and server["tool_profile"] == "compact"


def test_oneshot_commands_disabled_entries_and_relative_python_are_reported(tmp_path):
    report = doctor.inspect_client_config(write_config(tmp_path, {
        "command": "python", "args": ["-m", "solidworks_mcp_pro", "--doctor"], "enabled": False}))
    found = {issue["code"] for issue in report["servers"][0]["issues"]}
    assert {"CLIENT_ONE_SHOT_COMMAND", "CLIENT_SERVER_DISABLED", "CLIENT_PYTHON_UNRESOLVED"} <= found


@pytest.mark.parametrize("contents", ['{"secret-token-123456":', 'secret = "secret-token-123456"\n[bad'])
def test_parse_errors_do_not_echo_file_content(tmp_path, contents):
    path = tmp_path / ("config.toml" if contents.startswith("secret") else "config.json")
    path.write_text(contents, encoding="utf-8")
    report = doctor.inspect_client_config(path)
    assert report["status"] == "unreadable"
    assert "secret-token-123456" not in json.dumps(report)


def test_bounded_config_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "MAX_CONFIG_BYTES", 8)
    path = tmp_path / "big.json"
    path.write_text("{" + " " * 30, encoding="utf-8")
    assert doctor.inspect_client_config(path)["status"] == "unreadable"


def test_client_issues_affect_overall_status(environment, tmp_path):
    settings = pro_settings(environment, profile="full")
    report = full_report(environment, client_configs=[write_config(tmp_path, settings)], expected_client_profile="compact")
    assert report["status"] == "mismatch" and "CLIENT_PROFILE_MISMATCH" in codes(report)


def test_runtime_identity_has_no_backend_import_and_reports_missing_packages(monkeypatch):
    def version(name):
        if name == "solidworks-mcp-pro":
            return doctor.__version__
        raise doctor.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(doctor.metadata, "version", version)
    identity = doctor.runtime_identity()
    assert identity["python_executable"] == sys.executable
    assert identity["package_versions"]["solidworks-mcp-pro"] == doctor.__version__
    assert identity["package_versions"]["solidworks-mcp"] is None


def test_cli_is_offline_does_not_import_native_backend_or_execute_client(tmp_path):
    marker = tmp_path / "not_executed"
    config = write_config(tmp_path, {"command": sys.executable, "args": ["-c", f"open({str(marker)!r}, 'w').write('bad')"]})
    code = (
        "import sys; from solidworks_mcp_pro.connection_doctor import main; "
        f"result = main(['--client-config', {str(config)!r}]); "
        "assert 'solidworks_mcp.server' not in sys.modules; "
        "assert 'pythoncom' not in sys.modules; sys.exit(result)"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode in {1, 2}, result.stderr
    report = json.loads(result.stdout)
    assert report["read_only"] and not report["configured_commands_executed"]
    assert not marker.exists()
    assert "open(" not in result.stdout


def test_cli_accepts_observed_catalogue_without_connecting(environment, tmp_path, capsys):
    compact = sorted(doctor._compact_names())
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"published_tools": compact}), encoding="utf-8")
    assert doctor.main(["--source-root", str(environment[0]), "--tool-profile", "compact", "--tool-catalogue", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_cli_invalid_catalogue_never_reflects_input(tmp_path, capsys):
    path = tmp_path / "tools.json"
    path.write_text("private-token-not-json", encoding="utf-8")
    assert doctor.main(["--tool-catalogue", str(path)]) == 2
    output = capsys.readouterr().out
    assert "TOOL_CATALOGUE_UNREADABLE" in output and "private-token-not-json" not in output
