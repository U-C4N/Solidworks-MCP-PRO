"""Video tool schema, routing, worker and async lease tests; no CAD calls."""
import asyncio
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from solidworks_mcp_pro import bending_bridge, named_camera_bridge, pose_bridge, project_render_bridge, visibility_bridge
from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro.server import ProApplication


ASSEMBLY = r"C:\Owned\press.sldasm"
PART = r"C:\Owned\sheet.sldprt"
PROJECT = r"C:\Owned\scene.svpj"
VISIBILITY = {"document_path": ASSEMBLY, "components": [{"component": "Sheet-1", "visible": True}]}
BEND = {"document_path": PART, "angle_deg": 45, "suppress_features": ["Flat"]}
CAMERA = {"document_path": ASSEMBLY, "action": "apply", "name": "MCP_Camera-1",
          "position_mm": [200, 100, 300], "target_mm": [0, 40, 0]}
PROJECT_RENDER = {"request_id": "project-fixture", "project_path": PROJECT, "width": 128, "height": 128}
SYNC_ROUTES = [
    ("pro_assembly_visibility", "animation", "visibility", VISIBILITY),
    ("pro_sheet_bend", "animation", "bend", BEND),
    ("pro_visualize_named_camera", "visualize", "named_camera", CAMERA),
]


class Backend:
    instructions = "Offline V2 integration fixture."

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.gate = None
        self.result = {"ok": True, "verified": True}

    async def list_tools(self):
        return []

    def guide(self):
        return "Offline fixture"

    async def call(self, name, arguments):
        pytest.fail("V2 extensions must use their dedicated backend routes")

    async def animation(self, operation, **arguments):
        self.calls.append(("animation", operation, deepcopy(arguments)))
        return deepcopy(self.result)

    async def visualize(self, operation, **arguments):
        self.calls.append(("visualize", operation, deepcopy(arguments)))
        if operation != "render_project":
            return deepcopy(self.result)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if not self.result["ok"]:
            return deepcopy(self.result)
        directory = Path(arguments["output_directory"])
        assert not directory.exists()
        directory.mkdir()
        image = directory / "render.png"
        Image.new("RGB", (arguments.get("width", 1920), arguments.get("height", 1080)), "gray").save(image)
        return {"ok": True, "images": [{"path": str(image)}], "renderer": {"native_execution": True}}


@pytest.fixture
async def application(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "leases"))
    backend = Backend()
    app = ProApplication(backend, tmp_path / "workspace")
    await app.initialize()
    yield app, backend
    if backend.gate is not None:
        backend.gate.set()
    await app.registry.drain()


@pytest.mark.parametrize("tool,kind,operation,arguments", SYNC_ROUTES)
async def test_sync_routes_exact_arguments(application, tool, kind, operation, arguments):
    app, backend = application
    result = await app.call(tool, deepcopy(arguments))
    assert not result.isError and result.structuredContent["operation_status"] == "completed"
    assert backend.calls == [(kind, operation, arguments)]


async def test_new_video_catalog_annotations_and_strict_kwargs(application):
    app, _ = application
    tools = {tool.name: tool for tool in app.tools}
    for name in [*(row[0] for row in SYNC_ROUTES), "pro_visualize_render_project"]:
        assert tools[name].inputSchema["additionalProperties"] is False
        assert not tools[name].annotations.readOnlyHint
        assert tools[name].annotations.destructiveHint is (name != "pro_visualize_render_project")


@pytest.mark.parametrize("tool,arguments", [(row[0], row[3]) for row in SYNC_ROUTES] + [("pro_visualize_render_project", PROJECT_RENDER)])
async def test_unknown_arguments_are_rejected_before_backend(application, tool, arguments):
    app, backend = application
    result = await app.call(tool, {**arguments, "arbitrary_code": "not permitted"})
    assert result.isError and result.structuredContent["error_type"] == "ValidationError"
    assert backend.calls == []


@pytest.mark.parametrize("tool,arguments", [
    ("pro_visualize_render_project", {**PROJECT_RENDER, "width": 64}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "height": 127}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "width": 4097}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "passes": 5001}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "denoiser": 1}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "camera_position_mm": [1, 2, 3]}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "camera_target_mm": [1, 2, 3]}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "ground_reflections": True, "ground_shadows": False}),
    ("pro_visualize_render_project", {**PROJECT_RENDER, "vertical_fov_deg": 151}),
    ("pro_sheet_bend", {**BEND, "angle_deg": 0.005}),
    ("pro_sheet_bend", {**BEND, "angle_deg": -1}),
    ("pro_sheet_bend", {**BEND, "angle_deg": 91}),
    ("pro_sheet_bend", {**BEND, "suppress_features": []}),
    ("pro_sheet_bend", {**BEND, "suppress_features": ["Flat", "Flat"]}),
    ("pro_sheet_bend", {**BEND, "suppress_features": ["x" * 257]}),
    ("pro_sheet_bend", {**BEND, "configuration": "Original"}),
    ("pro_sheet_bend", {**BEND, "configuration": "MCP_Bend" + "a" * 81}),
    ("pro_sheet_bend", {**BEND, "bottom_offset_mm": 1001}),
    ("pro_assembly_visibility", {"document_path": ASSEMBLY, "components": []}),
    ("pro_assembly_visibility", {"document_path": ASSEMBLY, "components": [], "dry_run": False}),
    ("pro_assembly_visibility", {"document_path": ASSEMBLY, "components": [{"component": "Sheet-1", "visible": 1}]}),
    ("pro_visualize_named_camera", {"document_path": ASSEMBLY, "action": "apply"}),
    ("pro_visualize_named_camera", {"document_path": ASSEMBLY, "action": "apply", "position_mm": [1, 2, 3]}),
    ("pro_visualize_named_camera", {"document_path": ASSEMBLY, "position_mm": [1, 2, 3]}),
    ("pro_visualize_named_camera", {"document_path": ASSEMBLY, "action": "delete", "activate": False}),
    ("pro_visualize_named_camera", {"document_path": ASSEMBLY, "action": "inspect", "roll_deg": 1}),
    ("pro_visualize_named_camera", {**CAMERA, "name": "Unowned"}),
    ("pro_visualize_named_camera", {**CAMERA, "position_mm": [0, 0]}),
])
async def test_invalid_shapes_ranges_and_relations_fail_before_backend(application, tool, arguments):
    app, backend = application
    result = await app.call(tool, arguments)
    assert result.isError and result.structuredContent["error_type"] == "ValidationError"
    assert backend.calls == []


@pytest.mark.parametrize("angle", [0, 0.01, 90])
async def test_valid_bend_boundaries_and_owned_hyphens_route(application, angle):
    app, backend = application
    arguments = {**BEND, "angle_deg": angle, "configuration": "MCP_Bend-Test", "feature_name": "MCP_Bend-Body"}
    assert not (await app.call("pro_sheet_bend", arguments)).isError
    assert backend.calls == [("animation", "bend", arguments)]


async def test_visibility_empty_inventory_routes_only_with_dry_run(application):
    app, backend = application
    arguments = {"document_path": ASSEMBLY, "components": [], "dry_run": True}
    assert not (await app.call("pro_assembly_visibility", arguments)).isError
    assert backend.calls == [("animation", "visibility", arguments)]


async def test_project_async_replay_image_registration_and_unique_output(application):
    app, backend = application
    backend.gate = asyncio.Event()
    first = await app.call("pro_visualize_render_project", PROJECT_RENDER)
    assert first.structuredContent["operation_status"] == "running"
    await asyncio.wait_for(backend.started.wait(), 1)
    again = await app.call("pro_visualize_render_project", PROJECT_RENDER)
    assert again.structuredContent["operation_id"] == first.structuredContent["operation_id"]
    assert len(backend.calls) == 1
    kind, operation, arguments = backend.calls[0]
    assert (kind, operation) == ("visualize", "render_project") and "request_id" not in arguments
    assert Path(arguments["output_directory"]).parent == app.store.root
    assert not Path(arguments["output_directory"]).exists()
    backend.gate.set()
    completed = await app.call("pro_operation", {"operation_id": first.structuredContent["operation_id"], "wait_seconds": 1})
    assert not completed.isError and completed.structuredContent["operation_status"] == "completed"
    assert len(completed.structuredContent["image_artifacts"]) == 1
    assert any(block.type == "image" for block in completed.content)
    changed = await app.call("pro_visualize_render_project", {**PROJECT_RENDER, "width": 256})
    assert changed.isError and len(backend.calls) == 1


async def test_project_render_keeps_serial_lease_until_complete(application):
    app, backend = application
    backend.gate = asyncio.Event()
    started = await app.call("pro_visualize_render_project", PROJECT_RENDER)
    await asyncio.wait_for(backend.started.wait(), 1)
    other = asyncio.create_task(app.call("pro_assembly_visibility", VISIBILITY))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(backend.calls) == 1
    backend.gate.set()
    completed = await app.call("pro_operation", {"operation_id": started.structuredContent["operation_id"], "wait_seconds": 1})
    assert not completed.isError and not (await other).isError
    assert [item[1] for item in backend.calls] == ["render_project", "visibility"]


async def test_project_failure_preserves_native_evidence(application):
    app, backend = application
    backend.result = {"ok": False, "code": "NATIVE_RENDER_FAILED", "owned_log": "renderer.stderr.log"}
    started = await app.call("pro_visualize_render_project", PROJECT_RENDER)
    completed = await app.call("pro_operation", {"operation_id": started.structuredContent["operation_id"], "wait_seconds": 1})
    assert completed.isError and completed.structuredContent["code"] == "NATIVE_RENDER_FAILED"
    assert completed.structuredContent["owned_log"] == "renderer.stderr.log"


class Worker:
    def __init__(self):
        self.inside = False

    def submit(self, callback):
        future = Future()
        self.inside = True
        try:
            future.set_result(callback())
        except Exception as exc:
            future.set_exception(exc)
        finally:
            self.inside = False
        return future


BACKEND_ROUTES = [
    ("animation", "pose", pose_bridge, "pose", {"document_path": ASSEMBLY, "transforms": [], "dry_run": True}),
    ("animation", "visibility", visibility_bridge, "visibility", VISIBILITY),
    ("animation", "bend", bending_bridge, "apply", BEND),
    ("visualize", "named_camera", named_camera_bridge, "configure", CAMERA),
    ("visualize", "render_project", project_render_bridge, "render", {"project_path": PROJECT, "output_directory": r"C:\Owned\NewOutput"}),
]


@pytest.mark.parametrize("kind,operation,module,method,arguments", BACKEND_ROUTES)
async def test_native_routes_stay_inside_existing_worker(monkeypatch, kind, operation, module, method, arguments):
    worker, app, calls = Worker(), object(), []

    def ensure():
        assert worker.inside
        calls.append("ensure")
        return app

    def require_model():
        assert worker.inside
        calls.append("model")

    def busy(flag):
        assert worker.inside
        calls.append(("busy", flag))
        return False if flag is True else None

    def target(*positional, **kwargs):
        assert worker.inside and kwargs == arguments
        assert positional == (() if operation == "render_project" else (app,))
        calls.append("bridge")
        return {"ok": True}

    monkeypatch.setattr(module, method, target)
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker, _session=SimpleNamespace(_ensure=ensure, _require_model=require_model,
                                                                               _command_in_progress=busy))
    assert (await getattr(backend, kind)(operation, **arguments))["ok"]
    assert calls == ["ensure", *([] if operation == "render_project" else ["model"]),
                     *([("busy", True)] if operation in {"bend", "pose", "visibility"} else []), "bridge",
                     *([("busy", False)] if operation in {"bend", "pose", "visibility"} else [])]


@pytest.mark.parametrize("kind,operation,_,__,arguments", BACKEND_ROUTES)
async def test_native_routes_require_worker_without_cad_launch(kind, operation, _, __, arguments):
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=None)
    assert (await getattr(backend, kind)(operation, **arguments))["ok"] is False


@pytest.mark.parametrize("operation,module,method,arguments", [
    ("pose", pose_bridge, "pose", {"document_path": ASSEMBLY, "transforms": [], "dry_run": True}),
    ("visibility", visibility_bridge, "visibility", VISIBILITY),
    ("bend", bending_bridge, "apply", BEND),
])
@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("outcome", ["success", "reported_failure", "exception"])
async def test_animation_restores_exact_prior_busy_mode(monkeypatch, operation, module, method, arguments, previous, outcome):
    flags, state = [], {"busy": previous}
    worker = Worker()

    def busy(value):
        assert worker.inside
        old = state["busy"]
        flags.append(value)
        state["busy"] = value
        return old

    def native(*args, **kwargs):
        assert worker.inside and state["busy"] is True
        if outcome == "exception":
            raise RuntimeError("Native animation failure")
        return {"ok": outcome == "success", "verified": outcome == "success"}

    monkeypatch.setattr(module, method, native)
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker, _session=SimpleNamespace(
        _ensure=lambda: object(), _require_model=lambda: None,
        _command_in_progress=busy))
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="Native animation failure"):
            await backend.animation(operation, **arguments)
    else:
        assert (await backend.animation(operation, **arguments))["ok"] is (outcome == "success")
    assert flags == [True, previous]
    assert state["busy"] is previous
