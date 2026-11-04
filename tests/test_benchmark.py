import importlib.util
import json
from pathlib import Path
import sys

from mcp import types
import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("benchmark_" + name, ROOT / "benchmarks" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


protocol = load("run_protocol")
reporter = load("report")


@pytest.fixture
def server_spec(tmp_path):
    script = tmp_path / "fixture_server.py"
    script.write_text('''
import asyncio, os
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
server = Server("benchmark-fixture", version="1.0")
@server.list_tools()
async def tools():
    with Path(os.environ["LIST_MARKER"]).open("a") as stream:
        stream.write("listed\\n")
    return [
        types.Tool(name="echo", description="Echo a supplied value.", inputSchema={"type":"object","properties":{},"additionalProperties":False}, annotations=types.ToolAnnotations(readOnlyHint=True)),
        types.Tool(name="unannotated", inputSchema={"type":"object"}),
        types.Tool(name="invalid", description="Invalid schema fixture", inputSchema={"type":"not-a-json-schema-type"}),
    ]
@server.call_tool()
async def call(name, arguments):
    Path(os.environ["CALL_MARKER"]).write_text(name)
    return [types.TextContent(type="text",text='{"ok":true,"value":7}')]
async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
asyncio.run(main())
''', encoding="utf-8")
    return {"id": "fixture", "name": "Fixture", "command": sys.executable, "args": [str(script)],
            "env": {"LIST_MARKER": str(tmp_path / "listed.txt"), "CALL_MARKER": str(tmp_path / "called.txt")},
            "source": {"repository": "https://example.org/test", "revision": "test-fixture", "license": "MIT"}}


async def test_real_stdio_discovery_repeats_and_never_calls_tools(server_spec, tmp_path):
    result = await protocol.measure_server(server_spec, repeats=5, catalog_dir=tmp_path / "catalogs")
    assert result["status"] == "measured", result
    assert result["cad_tools_invoked"] == 0
    assert result["initialization"]["elapsed_ms"] > 0
    assert result["tools_list"]["sample_count"] == 5
    assert result["tools_list"]["tool_count"] == 3
    assert result["tools_list"]["stable_tool_count"]
    assert result["tools_list"]["schema_validation"]["status"] == "fail"
    assert result["tools_list"]["schema_validation"]["invalid"][0]["tool"] == "invalid"
    assert result["tools_list"]["missing_descriptions"] == ["unannotated"]
    assert result["tools_list"]["missing_annotations"] == ["unannotated", "invalid"]
    assert result["tools_list"]["unknown_argument_handling"]["status"] == "not_run"
    assert result["resources"]["status"] == "unsupported"
    assert result["prompts"]["status"] == "unsupported"
    assert Path(server_spec["env"]["LIST_MARKER"]).read_text().splitlines() == ["listed"] * 5
    assert not Path(server_spec["env"]["CALL_MARKER"]).exists()
    snapshot = json.loads((tmp_path / "catalogs" / "catalog-fixture.json").read_text())
    assert len(snapshot["tools"]) == 3 and snapshot["source"]["revision"] == "test-fixture"


async def test_exported_connect_and_timed_call_on_fixture_only(server_spec):
    async with protocol.connect(server_spec) as session:
        assert session.benchmark_initialize["response"]["serverInfo"]["name"] == "benchmark-fixture"
        result = await protocol.timed_call(session, "echo", {})
        assert result["status"] == "pass"
        assert result["is_error"] is False
        assert result["output"] == {"ok": True, "value": 7}
        assert result["elapsed_ms"] >= 0


@pytest.mark.parametrize("envelope,expected,error", [
    ({"content": [{"type": "text", "text": '{"ok":false,"error":"failed"}'}]}, {"ok": False, "error": "failed"}, True),
    ({"structuredContent": {"value": 3}, "content": [], "isError": True}, {"value": 3}, True),
    ({"content": [{"type": "text", "text": '{"result":{"structuredContent":{"ok":true,"value":9}}}'}]}, {"ok": True, "value": 9}, False),
    ({"content": [{"type": "text", "text": 'plain text'}], "isError": False}, {"content": [{"type": "text", "text": 'plain text'}], "isError": False}, False),
    ({"structuredContent": {"content": [{"type": "text", "text": '{"message":"bad"}'}], "isError": True}}, {"message": "bad"}, True),
    ({"content": [{"type": "text", "text": '{"success":false,"error":"native failure"}'}]}, {"success": False, "error": "native failure"}, True),
    ({"structuredContent": {"success": True, "query_data": {"success": False, "status": "fail"}}}, {"success": True, "query_data": {"success": False, "status": "fail"}}, False),
])
def test_normalize_structured_text_and_nested_results(envelope, expected, error):
    result = protocol.normalize_result(envelope)
    assert result == {"is_error": error, "output": expected}


async def test_timeout_is_indeterminate_and_never_retried():
    class Session:
        calls = 0

        async def call_tool(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("request timed out")

    session = Session()
    result = await protocol.timed_call(session, "native", {})
    assert result["status"] == "fail" and result["is_error"]
    assert result["outcome_may_be_indeterminate"] and not result["automatic_retry"]
    assert session.calls == 1


async def test_disabled_and_bad_executable_do_not_become_passes(tmp_path):
    disabled = await protocol.measure_server({"id": "disabled", "enabled": False, "not_run_reason": "Dependency missing"})
    assert disabled["status"] == "not_run"
    assert disabled["reason"] == "Dependency missing"
    failure = await protocol.measure_server({"id": "broken", "command": str(tmp_path / "missing.exe")})
    assert failure["status"] == "fail" and failure["cad_tools_invoked"] == 0


def test_sanitize_paths_preserves_public_sources():
    data = {"path": "C:\\Users\\Private\\Project\\out.stl", "nested": ["C:/Users/Private/Project/report.json"], "url": "https://github.com/owner/repo"}
    result = protocol.sanitize_report(data, {"C:\\Users\\Private\\Project": "<REPO>"})
    assert result["path"] == "<REPO>\\out.stl"
    assert result["nested"] == ["<REPO>/report.json"]
    assert result["url"] == data["url"]


def test_statistics_and_report_keep_statuses_explicit():
    stats = protocol.statistics_ms([9, 1, 3, 5, 7])
    assert stats["median_ms"] == 5 and stats["min_ms"] == 1 and stats["max_ms"] == 9
    report = {"servers": [{"name": "Disabled | server", "status": "not_run", "reason": "No install", "source": {}},
                           {"name": "Broken", "status": "fail", "error": "Startup failed", "source": {}}]}
    text = reporter.render_markdown(report)
    assert "Disabled \\| server" in text
    assert "not_run" in text and "No install" in text and "Startup failed" in text
    assert "not an overall quality ranking" in text


async def test_runtime_mode_is_preserved_and_not_mislabeled_native(server_spec):
    server_spec["runtime_mode"] = "mock discovery only"
    result = await protocol.measure_server(server_spec, repeats=1)
    assert result["runtime_mode"] == "mock discovery only"
    text = reporter.render_markdown({"servers": [result]})
    assert "mock discovery only" in text
    assert "not native CAD evidence" in text


async def test_manifest_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="unique"):
        await protocol.run_manifest({"schema_version": 1, "servers": [{"id": "same"}, {"id": "same"}]})


async def test_required_baseline_file_prevents_silent_current_fallback(server_spec, tmp_path):
    server_spec["required_files"] = [str(tmp_path / "missing-frozen" / "__init__.py")]
    result = await protocol.measure_server(server_spec, repeats=1)
    assert result["status"] == "fail"
    assert "Required file missing" in result["error"]
    assert not Path(server_spec["env"]["LIST_MARKER"]).exists()
    required = tmp_path / "frozen-module.py"
    required.write_text("# frozen fixture", encoding="utf-8")
    server_spec["required_files"] = [str(required)]
    assert (await protocol.measure_server(server_spec, repeats=1))["status"] == "measured"


async def test_required_files_must_be_absolute(server_spec):
    server_spec["required_files"] = ["relative/frozen.py"]
    result = await protocol.measure_server(server_spec)
    assert result["status"] == "fail" and "must be absolute" in result["error"]


def test_schema_sizes_are_utf8_bytes_and_unknown_policy_is_static():
    tool = types.Tool(name="unicode", description="Çizim", inputSchema={"type": "object", "properties": {"ç": {"type": "string"}}, "additionalProperties": False})
    result = protocol.inspect_catalog([tool])
    expected = len(json.dumps([tool.inputSchema], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert result["input_schema_bytes"] == expected
    assert result["unknown_argument_handling"]["static_top_level_additional_properties"]["explicitly_forbidden"] == 1
