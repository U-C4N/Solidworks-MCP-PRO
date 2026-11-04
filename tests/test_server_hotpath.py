"""The cached wire-validation path must retain every safety boundary."""
import asyncio
from copy import deepcopy
from pathlib import Path

from mcp import types
import pytest

from solidworks_mcp_pro.server import ProApplication
from solidworks_mcp_pro.storage import CadBusyError, CadLease


class Backend:
    instructions = "Synthetic validation backend. No COM."

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def list_tools(self):
        properties = {
            "new_part": {}, "slow_tool": {},
            "add_box": {key: {"type": "number"} for key in ("width_mm", "height_mm", "depth_mm")},
            "add_extruded_profile": {"points_mm": {"type": "array"}, "depth_mm": {"type": "number"}},
            "set_dimension": {"dimension_name": {"type": "string"}, "value_mm": {"type": "number"}},
            "save_part": {"path": {"type": "string"}},
        }
        return [types.Tool(name=name, inputSchema={"type": "object", "properties": props,
                                                   "required": list(props)}) for name, props in properties.items()]

    async def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == "slow_tool":
            self.started.set()
            await self.release.wait()
        if name == "set_dimension":
            return {"ok": True, "rebuild_ok": False, "broken_features": [{"name": "Cut1"}]}
        if name == "save_part":
            Path(arguments["path"]).write_bytes(b"replacement")
        return {"ok": True}


@pytest.fixture
async def application(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "leases"))
    backend = Backend()
    app = ProApplication(backend, tmp_path / "workspace")
    await app.initialize()
    return app, backend


async def wire(app, name, arguments):
    request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
    result = await app.server.request_handlers[types.CallToolRequest](request)
    return result.root


async def test_cached_wire_path_still_validates_each_instance_without_sdk_recompilation(application, monkeypatch):
    app, backend = application

    def unexpected_compilation(*args, **kwargs):
        raise AssertionError("Request path must not recompile/check the tool schema")

    monkeypatch.setattr("jsonschema.validate", unexpected_compilation)
    monkeypatch.setattr("solidworks_mcp_pro.server.Draft202012Validator", unexpected_compilation)
    valid = {"width_mm": 80, "height_mm": 50, "depth_mm": 8}
    assert not (await wire(app, "add_box", valid)).isError
    for invalid in ({**valid, "depth_mm": 0}, {**valid, "unexpected": 1}, {**valid, "width_mm": True}):
        failed = await wire(app, "add_box", invalid)
        assert failed.isError
        assert failed.structuredContent["error_type"] == "ValidationError"
    assert len(backend.calls) == 1


@pytest.mark.parametrize("coordinate", ["NaN", True, float("nan"), 10 ** 309])
async def test_wire_rejects_nested_invalid_geometry_before_backend(application, coordinate):
    app, backend = application
    result = await wire(app, "add_extruded_profile", {"points_mm": [[0, 0], [10, 0], [coordinate, 5]], "depth_mm": 5})
    assert result.isError and backend.calls == []


async def test_wire_plan_preserves_full_preflight_and_existing_output(application, tmp_path):
    app, backend = application
    output = tmp_path / "existing.sldprt"
    output.write_bytes(b"original")
    result = await wire(app, "pro_run_plan", {"request_id": "no-clobber", "dry_run": False, "steps": [
        {"tool": "new_part", "arguments": {}},
        {"tool": "save_part", "arguments": {"path": str(output.with_suffix(""))}},
    ]})
    assert result.isError and backend.calls == []
    assert output.read_bytes() == b"original"


async def test_wire_plan_stops_when_rebuild_postcondition_fails(application):
    app, backend = application
    result = await wire(app, "pro_run_plan", {"request_id": "failed-rebuild", "dry_run": False, "steps": [
        {"tool": "set_dimension", "arguments": {"dimension_name": "D1@Sketch1", "value_mm": 10}},
        {"tool": "new_part", "arguments": {}},
    ]})
    assert result.isError and [name for name, _ in backend.calls] == ["set_dimension"]
    assert result.structuredContent["steps"][0]["result"]["error_code"] == "CAD_POSTCONDITION_FAILED"


async def test_wire_cancellation_keeps_operation_and_cad_lease(application):
    app, backend = application
    waiter = asyncio.create_task(wire(app, "slow_tool", {}))
    await backend.started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(CadBusyError):
        with CadLease():
            pass
    operation_id = app.registry.list_recent()[0]["operation_id"]
    backend.release.set()
    result = await wire(app, "pro_operation", {"operation_id": operation_id, "wait_seconds": 1})
    assert not result.isError
    with CadLease():
        pass
