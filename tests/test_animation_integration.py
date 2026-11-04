"""Assembly animation MCP contracts with fake backends/workers; no live CAD."""

import asyncio
from concurrent.futures import Future
from copy import deepcopy
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import animation_bridge, pose_bridge
from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro.server import ProApplication


DOCUMENT = r"C:\Models\Animation_press.sldasm"
OUTPUT = r"C:\Models\NewAnimationCopy"
POSE = {"component": "Handle-1", "position_mm": [10, 20, 30], "rotation_deg": [0, 45, 0]}
ROUTES = [
    ("pro_assembly_configurations", "inspect", {"document_path": DOCUMENT}),
    ("pro_assembly_configure", "configure", {"document_path": DOCUMENT, "configuration": "Acik"}),
    ("pro_assembly_clone", "clone", {"document_path": DOCUMENT, "output_directory": OUTPUT}),
    ("pro_assembly_pose", "pose", {"document_path": DOCUMENT, "transforms": [POSE], "dry_run": True}),
]


class FakeBackend:
    instructions = "Deterministic animation test backend."

    def __init__(self):
        self.calls = []
        self.result = {"ok": True, "verified": True}
        self.started = asyncio.Event()
        self.release = None

    async def list_tools(self):
        return []

    def guide(self):
        return "Fake assembly guide"

    async def call(self, name, arguments):
        pytest.fail("Animation PRO tools must route to backend.animation, not native-tool call")

    async def animation(self, operation, **arguments):
        self.calls.append((operation, deepcopy(arguments)))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return deepcopy(self.result)


@pytest.fixture
async def application(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    backend = FakeBackend()
    app = ProApplication(backend, tmp_path / "workspace")
    await app.initialize()
    return app, backend


@pytest.mark.parametrize("tool,operation,arguments", ROUTES)
async def test_animation_tools_route_exact_arguments(application, tool, operation, arguments):
    app, backend = application
    result = await app.call(tool, deepcopy(arguments))
    assert not result.isError
    assert result.structuredContent["operation_status"] == "completed"
    assert backend.calls == [(operation, arguments)]


async def test_four_tools_are_discoverable_with_correct_mutation_annotations(application):
    app, _ = application
    tools = {tool.name: tool for tool in app.tools}
    assert len(tools) == len(app.tools)
    for name, _, _ in ROUTES:
        assert name in tools
        assert tools[name].inputSchema["additionalProperties"] is False
        assert tools[name].annotations.readOnlyHint is (name == "pro_assembly_configurations")
    assert tools["pro_assembly_configure"].annotations.destructiveHint
    assert tools["pro_assembly_pose"].annotations.destructiveHint


@pytest.mark.parametrize("tool,operation,arguments", ROUTES)
async def test_unknown_top_level_arguments_rejected_before_backend(application, tool, operation, arguments):
    app, backend = application
    result = await app.call(tool, {**deepcopy(arguments), "run_code": "arbitrary"})
    assert result.isError
    assert result.structuredContent["error_type"] == "ValidationError"
    assert backend.calls == []


@pytest.mark.parametrize("tool,operation,arguments", ROUTES)
async def test_exact_document_argument_required_for_every_tool(application, tool, operation, arguments):
    app, backend = application
    invalid = deepcopy(arguments)
    del invalid["document_path"]
    result = await app.call(tool, invalid)
    assert result.isError
    assert backend.calls == []


async def test_component_configuration_mode_routes_without_assembly_configuration(application):
    app, backend = application
    arguments = {"document_path": DOCUMENT, "components": [
        {"component": "Spring-1", "configuration": "Basili"},
        {"component": "Sheet-1", "configuration": "Bukme45"},
    ]}
    result = await app.call("pro_assembly_configure", arguments)
    assert not result.isError
    assert backend.calls == [("configure", arguments)]


@pytest.mark.parametrize("fields", [
    {},
    {"configuration": "Acik", "components": [{"component": "Spring-1", "configuration": "Basili"}]},
    {"configuration": "Acik", "components": []},
    {"components": []},
    {"components": [{"component": "Spring-1"}]},
    {"components": [{"configuration": "Acik"}]},
    {"components": [{"component": "Spring-1", "configuration": "Basili", "save": True}]},
    {"components": [{"component": "Spring-1", "configuration": "Basili"}] * 129},
])
async def test_configuration_modes_and_nested_assignment_contract(application, fields):
    app, backend = application
    result = await app.call("pro_assembly_configure", {"document_path": DOCUMENT, **fields})
    assert result.isError
    assert backend.calls == []


@pytest.mark.parametrize("changes", [
    {"position_mm": [1000001, 0, 0]}, {"position_mm": [-1000001, 0, 0]},
    {"position_mm": [0, 0]}, {"position_mm": [0, 0, 0, 0]},
    {"position_mm": [True, 0, 0]},
    {"rotation_deg": [3601, 0, 0]}, {"rotation_deg": [-3601, 0, 0]},
    {"rotation_deg": [0, 0]}, {"rotation_deg": [0, 0, 0, 0]},
    {"rotation_deg": [0, False, 0]}, {"scale": 2},
])
async def test_pose_rejects_bad_ranges_shapes_and_unknown_fields(application, changes):
    app, backend = application
    arguments = {"document_path": DOCUMENT, "transforms": [{**POSE, **changes}]}
    result = await app.call("pro_assembly_pose", arguments)
    assert result.isError
    assert backend.calls == []


@pytest.mark.parametrize("changes", [
    {"transforms": [POSE] * 129}, {"transforms": [{}]}, {"dry_run": 1},
])
async def test_pose_batch_limits_and_flags(application, changes):
    app, backend = application
    arguments = {"document_path": DOCUMENT, "transforms": [POSE], **changes}
    result = await app.call("pro_assembly_pose", arguments)
    assert result.isError
    assert backend.calls == []


async def test_pose_empty_dry_run_inventory_and_boundary_values_route(application):
    app, backend = application
    inventory = {"document_path": DOCUMENT, "transforms": [], "dry_run": True}
    assert not (await app.call("pro_assembly_pose", inventory)).isError
    boundary = {"document_path": DOCUMENT, "transforms": [{
        "component": "Handle-1", "position_mm": [-1000000, 0, 1000000],
        "rotation_deg": [-3600, 0, 3600],
    }]}
    assert not (await app.call("pro_assembly_pose", boundary)).isError
    assert backend.calls == [("pose", inventory), ("pose", boundary)]


async def test_clone_requires_output_directory(application):
    app, backend = application
    result = await app.call("pro_assembly_clone", {"document_path": DOCUMENT})
    assert result.isError
    assert backend.calls == []


@pytest.mark.parametrize("tool,operation,arguments", ROUTES)
async def test_bridge_failure_is_mcp_failure_without_erasing_evidence(application, tool, operation, arguments):
    app, backend = application
    backend.result = {"ok": False, "code": "NATIVE_READBACK_FAILED", "partial_state_possible": True,
                      "rollback": {"attempted": True, "verified": False}}
    result = await app.call(tool, deepcopy(arguments))
    assert result.isError
    assert result.structuredContent["operation_status"] == "failed"
    assert result.structuredContent["partial_state_possible"]
    assert result.structuredContent["rollback"]["verified"] is False
    assert len(backend.calls) == 1


async def test_animation_calls_share_serial_cad_lease(application):
    app, backend = application
    backend.release = asyncio.Event()
    first = asyncio.create_task(app.call("pro_assembly_pose", {
        "document_path": DOCUMENT, "transforms": [], "dry_run": True,
    }))
    await asyncio.wait_for(backend.started.wait(), timeout=2)
    second = asyncio.create_task(app.call("pro_assembly_configurations", {"document_path": DOCUMENT}))
    try:
        # Let the second registered operation reach the async lease boundary.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [operation for operation, _ in backend.calls] == ["pose"]
    finally:
        backend.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert [operation for operation, _ in backend.calls] == ["pose", "inspect"]


class Worker:
    def __init__(self):
        self.inside = False
        self.submitted = 0

    def submit(self, callback):
        self.submitted += 1
        future = Future()
        self.inside = True
        try:
            future.set_result(callback())
        except Exception as exc:
            future.set_exception(exc)
        finally:
            self.inside = False
        return future


@pytest.mark.parametrize("operation,module,method,arguments", [
    ("inspect", animation_bridge, "inspect", {"document_path": DOCUMENT}),
    ("configure", animation_bridge, "configure", {"document_path": DOCUMENT, "configuration": "Acik"}),
    ("clone", animation_bridge, "clone", {"document_path": DOCUMENT, "output_directory": OUTPUT}),
    ("pose", pose_bridge, "pose", {"document_path": DOCUMENT, "transforms": [], "dry_run": True}),
])
async def test_native_backend_routes_only_inside_existing_cad_worker(monkeypatch, operation, module, method, arguments):
    worker = Worker()
    app = object()
    calls = []

    def ensure():
        assert worker.inside
        calls.append("ensure")
        return app

    def require_model():
        assert worker.inside
        calls.append("require_model")

    def bridge_call(actual_app, **kwargs):
        assert worker.inside and actual_app is app
        calls.append((method, kwargs))
        return {"ok": True}

    monkeypatch.setattr(module, method, bridge_call)
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker, _session=SimpleNamespace(
        _ensure=ensure, _require_model=require_model, _command_in_progress=lambda flag: False))
    assert (await backend.animation(operation, **arguments))["ok"]
    assert worker.submitted == 1
    assert calls == ["ensure", "require_model", (method, arguments)]


@pytest.mark.parametrize("operation", ["inspect", "configure", "clone", "pose"])
async def test_native_backend_requires_worker_without_opening_cad(operation):
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace()
    result = await backend.animation(operation, document_path=DOCUMENT)
    assert result["ok"] is False
    assert "requires Windows and SOLIDWORKS" in result["error"]


@pytest.mark.parametrize("operation,module,method", [
    ("inspect", animation_bridge, "inspect"), ("configure", animation_bridge, "configure"),
    ("clone", animation_bridge, "clone"), ("pose", pose_bridge, "pose"),
])
async def test_missing_active_session_model_prevents_bridge_call(monkeypatch, operation, module, method):
    worker = Worker()

    def require_model():
        raise RuntimeError("No active assembly")

    monkeypatch.setattr(module, method, lambda *args, **kwargs: pytest.fail("Must not reach bridge without model"))
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker, _session=SimpleNamespace(
        _ensure=lambda: object(), _require_model=require_model,
    ))
    with pytest.raises(RuntimeError, match="No active assembly"):
        await backend.animation(operation, document_path=DOCUMENT)
    assert worker.submitted == 1


async def test_unknown_backend_operation_cannot_call_arbitrary_bridge_attribute(monkeypatch):
    worker = Worker()
    monkeypatch.setattr(animation_bridge, "_active", lambda *args: pytest.fail("Private bridge method must be unreachable"))
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker, _session=SimpleNamespace(
        _ensure=lambda: object(), _require_model=lambda: None,
    ))
    with pytest.raises(ValueError, match="Unsupported assembly animation operation"):
        await backend.animation("_active", document_path=DOCUMENT)
