"""MCP integration contracts using a deterministic backend, plus real stdio discovery."""
import asyncio
import base64
import copy
import os
from pathlib import Path
import struct
import sys

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
import pytest

from solidworks_mcp_pro.server import ProApplication, pro_tools


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1sAAAAASUVORK5CYII=")


def make_stl(path):
    triangle = (0, 0, 0, 1, 0, 0, 0, 1, 0)
    path.write_bytes(b"integration-test".ljust(80, b" ") + struct.pack("<I", 1) + struct.pack("<12fH", 0, 0, 1, *triangle, 0))
    return path


class FakeBackend:
    instructions = "Test backend, lengths in mm."

    def __init__(self):
        self.calls = []
        self.visualize_calls = []
        self.render_gate = None
        self.native_gate = None
        self.native_started = asyncio.Event()
        self.failure = {"ok": False, "error": "TEST_NATIVE_FAILURE"}

    async def list_tools(self):
        names = ["get_status", "get_mass_properties", "screenshot", "export", "new_part", "extrude", "native_failure", "native_wait"]
        names += [f"native_tool_{i}" for i in range(80)]
        result = []
        for name in names:
            schema = {"type": "object", "properties": {}, "additionalProperties": False}
            if name == "extrude":
                schema["properties"] = {"depth_mm": {"type": "number", "exclusiveMinimum": 0}}
                schema["required"] = ["depth_mm"]
            result.append(types.Tool(name=name, inputSchema=schema))
        return result

    async def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "native_failure":
            return copy.deepcopy(self.failure)
        if name == "native_wait":
            self.native_started.set()
            await self.native_gate.wait()
        if name == "screenshot":
            Path(arguments["path"]).write_bytes(PNG)
        if name == "export":
            make_stl(Path(arguments["path"]))
        if name == "get_mass_properties":
            return {"ok": True, "mass_properties": {"volume_mm3": 1000}}
        return {"ok": True}

    def guide(self):
        return "# Fake native guide\nUse millimetres."

    async def visualize(self, operation, **arguments):
        self.visualize_calls.append((operation, arguments))
        if operation == "open_project":
            return {"ok": True, "status": "launch_requested", "project_path": arguments["path"], "project_loaded": None}
        if operation == "render":
            if self.render_gate is not None:
                await self.render_gate.wait()
            directory = Path(arguments["output_dir"])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "render.png"
            path.write_bytes(PNG)
            return {"ok": True, "images": [{"path": str(path)}]}
        if operation == "export_project":
            Path(arguments["path"]).write_bytes(b"fake-project")
        return {"ok": True, "available": True}


@pytest.fixture
async def application(tmp_path, monkeypatch):
    # Isolate cross-process leases from production or another test session.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    backend = FakeBackend()
    app = ProApplication(backend, tmp_path / "workspace")
    await app.initialize()
    return app, backend


async def read_resource(app, uri):
    request = types.ReadResourceRequest(params=types.ReadResourceRequestParams(uri=uri))
    response = await app.server.request_handlers[types.ReadResourceRequest](request)
    return response.root.contents[0]


async def test_catalog_capabilities_and_guides(application):
    app, backend = application
    assert len(app.tools) == 88 + len(pro_tools())
    assert len({tool.name for tool in app.tools}) == len(app.tools)
    result = await app.call("pro_capabilities", {})
    assert result.isError is False
    assert result.structuredContent["native_tool_count"] == 88
    assert result.structuredContent["pro_tool_count"] == len(pro_tools())
    assert result.structuredContent["transport"] == "stdio"
    assert not backend.calls
    assert (await read_resource(app, "solidworks://guide")).text == backend.guide()
    assert "PRO" in (await read_resource(app, "solidworks-pro://guide")).text


@pytest.mark.parametrize("failure", [{"ok": False, "error": "native failed"}, {"isError": True, "error": "native failed"}])
async def test_native_failure_sets_mcp_is_error(application, failure):
    app, backend = application
    backend.failure = failure
    result = await app.call("native_failure", {})
    assert result.isError is True
    assert result.structuredContent["operation_status"] == "failed"
    assert result.structuredContent["error"] == "native failed"


async def test_validation_rejects_entire_plan_before_any_cad_call(application):
    app, backend = application
    result = await app.call("pro_run_plan", {"request_id": "invalid-plan", "dry_run": False, "steps": [
        {"tool": "new_part", "arguments": {}},
        {"tool": "extrude", "arguments": {"depth_mm": -1}},
    ]})
    assert result.isError
    assert result.structuredContent["executed_steps"] == 0
    assert backend.calls == []
    dry = await app.call("pro_run_plan", {"request_id": "dry", "steps": [{"tool": "new_part", "arguments": {}}]})
    assert not dry.isError
    assert dry.structuredContent["dry_run"] is True
    assert backend.calls == []


async def test_offline_stl_inspection_and_html_resource(application, tmp_path):
    app, backend = application
    mesh = make_stl(tmp_path / "outside-output.stl")
    result = await app.call("pro_inspect_stl", {"path": str(mesh)})
    assert result.structuredContent["triangle_count"] == 1
    preview = await app.call("pro_preview_stl", {"path": str(mesh), "title": 'A </script> "model"'})
    assert not preview.isError
    resource = await read_resource(app, preview.structuredContent["resource_uri"])
    assert resource.mimeType == "text/html"
    assert '<canvas id="view"' in resource.text
    assert 'A </script> "model"' not in resource.text
    assert backend.calls == []


async def test_capture_returns_registered_image_content(application):
    app, backend = application
    result = await app.call("pro_capture_view", {"view": "front"})
    assert not result.isError
    assert backend.calls[0][0] == "screenshot"
    images = [block for block in result.content if isinstance(block, types.ImageContent)]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data) == PNG
    resource = await read_resource(app, result.structuredContent["uri"])
    assert base64.b64decode(resource.blob) == PNG


async def test_render_idempotency_poll_and_image_attachment(application):
    app, backend = application
    backend.render_gate = asyncio.Event()
    arguments = {"request_id": "render-once", "width": 64, "height": 64}
    first = await app.call("pro_visualize_render", arguments)
    second = await app.call("pro_visualize_render", arguments)
    assert first.structuredContent["operation_id"] == second.structuredContent["operation_id"]
    assert second.structuredContent["replayed"]
    assert len(backend.visualize_calls) == 1
    assert Path(backend.visualize_calls[0][1]["output_dir"]) != app.store.root
    conflict = await app.call("pro_visualize_render", {**arguments, "width": 128})
    assert conflict.isError and len(backend.visualize_calls) == 1
    backend.render_gate.set()
    result = await app.call("pro_operation", {"operation_id": first.structuredContent["operation_id"], "wait_seconds": 1})
    assert not result.isError
    assert result.structuredContent["operation_status"] == "completed"
    assert any(isinstance(block, types.ImageContent) and base64.b64decode(block.data) == PNG for block in result.content)


async def test_open_project_is_discoverable_and_launch_only(application):
    app, backend = application
    assert "pro_visualize_open_project" in app.schemas
    result = await app.call("pro_visualize_open_project", {"path": r"C:\Models\Assembly.svpj"})
    assert not result.isError
    assert result.structuredContent["status"] == "launch_requested"
    assert result.structuredContent["operation_status"] == "completed"
    assert result.structuredContent["project_loaded"] is None
    assert backend.visualize_calls == [("open_project", {"path": r"C:\Models\Assembly.svpj"})]
    assert not backend.calls
    rejected = await app.call("pro_visualize_open_project", {"path": "x", "executable": "cmd.exe"})
    assert rejected.isError and len(backend.visualize_calls) == 1


async def test_open_project_waits_for_active_cad_operation(application):
    app, backend = application
    backend.native_gate = asyncio.Event()
    native = asyncio.create_task(app.call("native_wait", {}))
    await backend.native_started.wait()
    opening = asyncio.create_task(app.call("pro_visualize_open_project", {"path": "test.svpj"}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert backend.visualize_calls == []
    backend.native_gate.set()
    native_result, opened = await asyncio.gather(native, opening)
    assert not native_result.isError and not opened.isError
    assert backend.visualize_calls == [("open_project", {"path": "test.svpj"})]


async def test_appearances_dispatch_exact_document_components_and_rgb(application):
    app, backend = application
    arguments = {"document_path": r"C:\Models\Assembly.sldasm", "assignments": [
        {"component": "Handle-1", "material_path": r"C:\Materials\metal.p2m", "color_rgb": [32, 64, 255]}]}
    result = await app.call("pro_visualize_appearances", arguments)
    assert not result.isError
    assert backend.visualize_calls == [("apply_appearances", {**arguments, "dry_run": False})]
    tool = next(tool for tool in app.tools if tool.name == "pro_visualize_appearances")
    assert not tool.annotations.readOnlyHint and tool.annotations.destructiveHint


async def test_appearances_dry_run_reaches_backend_unchanged(application):
    app, backend = application
    arguments = {"document_path": r"C:\Models\Assembly.sldasm", "assignments": [
        {"component": "Handle-1", "material_path": r"C:\Materials\metal.p2m"}], "dry_run": True}
    result = await app.call("pro_visualize_appearances", arguments)
    assert not result.isError
    assert backend.visualize_calls == [("apply_appearances", arguments)]


@pytest.mark.parametrize("tool, action, arguments", [
    ("pro_visualize_scene", "apply_scene", {"document_path": r"C:\Models\Assembly.sldasm", "dry_run": True}),
    ("pro_visualize_scene", "apply_scene", {"document_path": r"C:\Models\Assembly.sldasm",
        "disable_light_ids": [1, 2, 3], "disable_light_names": []}),
    ("pro_visualize_scene", "apply_scene", {"document_path": r"C:\Models\Assembly.sldasm",
        "scene_path": r"C:\Scenes\studio.p2s", "hdri_path": r"C:\Scenes\softboxes.hdr",
        "environment_rotation_deg": 45, "floor_shadows": True, "floor_reflections": False,
        "disable_light_names": ["Directional1"]}),
    ("pro_visualize_scene", "apply_scene", {"document_path": r"C:\Models\Assembly.sldasm",
        "hdri_path": r"C:\Scenes\softboxes.hdr",
        "background_type": "plain", "background_top_rgb": [240, 240, 240]}),
    ("pro_visualize_scene", "apply_scene", {"document_path": r"C:\Models\Assembly.sldasm",
        "background_type": "gradient",
        "background_top_rgb": [255, 255, 255], "background_bottom_rgb": [0, 0, 0]}),
    ("pro_visualize_camera", "configure_camera", {"document_path": r"C:\Models\Assembly.sldasm", "action": "inspect"}),
    ("pro_visualize_camera", "configure_camera", {"document_path": r"C:\Models\Assembly.sldasm",
        "action": "apply", "view": "custom", "from_direction": [1, -1, 1],
        "up_direction": [0, 1, 0], "fit": True, "zoom_factor": 0.85,
        "perspective": True, "object_sizes_away": 4}),
])
async def test_scene_camera_tools_forward_exact_validated_arguments(application, tool, action, arguments):
    app, backend = application
    result = await app.call(tool, arguments)
    assert not result.isError
    assert backend.visualize_calls == [(action, arguments)]
    catalog_tool = next(item for item in app.tools if item.name == tool)
    assert not catalog_tool.annotations.readOnlyHint and catalog_tool.annotations.destructiveHint


@pytest.mark.parametrize("arguments", [
    {"environment_rotation_deg": -1}, {"environment_rotation_deg": 361},
    {"environment_rotation_deg": float("nan")}, {"environment_rotation_deg": True},
    {"floor_reflections": True, "floor_shadows": False}, {"floor_shadows": "true"},
    {"disable_light_names": ["Light1", "Light1"]}, {"disable_light_names": [""]},
    {"disable_light_names": [f"Light{i}" for i in range(65)]},
    {"disable_light_ids": [1, 1]}, {"disable_light_ids": [-1]}, {"disable_light_ids": [256]},
    {"disable_light_ids": [True]}, {"disable_light_ids": [1.5]}, {"disable_light_ids": list(range(65))},
    {"disable_light_ids": [1], "disable_light_names": ["Light1"]},
    {"disable_light_ids": [1], "scene_path": r"C:\Scenes\studio.p2s"},
    {"hdri_path": None}, {"scene_path": ""}, {"brightness": 2},
    {"rendering_brightness": 2},
    {"background_type": "color"}, {"background_type": 4},
    {"background_top_rgb": [1, 2]}, {"background_bottom_rgb": [0, 0, 0, 0]},
    {"background_top_rgb": [True, 0, 0]}, {"background_bottom_rgb": [256, 0, 0]},
    {"background_top_rgb": [-1, 0, 0]}, {"background_bottom_rgb": [0, 0.5, 0]},
    {"background_type": "environment", "background_top_rgb": [240, 240, 240]},
    {"background_type": "plain", "background_bottom_rgb": [240, 240, 240]},
    {"hdri_path": r"C:\Scenes\softboxes.hdr", "background_top_rgb": [240, 240, 240]},
    {"scene_path": r"C:\Scenes\studio.p2s", "background_top_rgb": [240, 240, 240]},
])
async def test_invalid_scene_arguments_never_reach_cad(application, arguments):
    app, backend = application
    result = await app.call("pro_visualize_scene", {"document_path": r"C:\Models\Assembly.sldasm", **arguments})
    assert result.isError and not backend.visualize_calls


@pytest.mark.parametrize("arguments", [
    {"view": "iso"}, {"action": "create_named_camera"}, {"view": "custom"},
    {"from_direction": [1, 1, 1]}, {"up_direction": [0, 1, 0]},
    {"view": "custom", "from_direction": [1, 1]},
    {"view": "custom", "from_direction": [1, float("inf"), 1]},
    {"view": "custom", "from_direction": [1, "1", 1]},
    {"zoom_factor": 0.24}, {"zoom_factor": 2.01}, {"perspective": 1},
    {"object_sizes_away": 0}, {"object_sizes_away": 21}, {"fit": "true"},
    {"focal_length_mm": 50}, {"save": True},
])
async def test_invalid_camera_arguments_never_reach_cad(application, arguments):
    app, backend = application
    result = await app.call("pro_visualize_camera", {"document_path": r"C:\Models\Assembly.sldasm", **arguments})
    assert result.isError and not backend.visualize_calls


async def test_scene_and_camera_queue_behind_active_native_operation(application):
    app, backend = application
    backend.native_gate = asyncio.Event()
    native = asyncio.create_task(app.call("native_wait", {}))
    await backend.native_started.wait()
    scene = asyncio.create_task(app.call("pro_visualize_scene", {"document_path": "model.sldasm", "dry_run": True}))
    camera = asyncio.create_task(app.call("pro_visualize_camera", {"document_path": "model.sldasm", "action": "inspect"}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not backend.visualize_calls
    backend.native_gate.set()
    results = await asyncio.gather(native, scene, camera)
    assert all(not result.isError for result in results)
    assert [action for action, _ in backend.visualize_calls] == ["apply_scene", "configure_camera"]


async def test_cancelled_scene_request_keeps_operation_and_lease(application):
    from solidworks_mcp_pro.storage import CadBusyError, CadLease

    app, backend = application
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def apply_scene(action, **arguments):
        calls.append(action)
        started.set()
        await release.wait()
        return {"ok": True, "scene_applied": True}

    backend.visualize = apply_scene
    waiter = asyncio.create_task(app.call("pro_visualize_scene", {
        "document_path": "model.sldasm", "environment_rotation_deg": 30}))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(CadBusyError):
        with CadLease():
            pass
    operation_id = app.registry.list_recent()[0]["operation_id"]
    release.set()
    result = await app.call("pro_operation", {"operation_id": operation_id, "wait_seconds": 1})
    assert not result.isError and result.structuredContent["scene_applied"]
    assert calls == ["apply_scene"]


@pytest.mark.parametrize("changes", [
    {"assignments": []}, {"assignments": [{}]},
    {"assignments": [{"component": "Handle-1", "material_path": "metal.p2m", "color_rgb": [1, 2, True]}]},
    {"assignments": [{"component": "Handle-1", "material_path": "metal.p2m", "color_rgb": [1, 2, 256]}]},
    {"assignments": [{"component": "Handle-1", "material_path": "metal.p2m", "color_rgb": [1, 2]}]},
    {"assignments": [{"component": "Handle-1", "material_path": "metal.p2m"}] * 129},
    {"scene_path": "unrequested.hdr"}, {"save": True}, {"dry_run": "true"},
])
async def test_invalid_appearance_arguments_do_not_reach_backend(application, changes):
    app, backend = application
    result = await app.call("pro_visualize_appearances", {
        "document_path": r"C:\Models\Assembly.sldasm", "assignments": [
            {"component": "Handle-1", "material_path": "metal.p2m"}], **changes})
    assert result.isError
    assert not backend.visualize_calls


async def test_volume_mismatch_is_mcp_error(application):
    app, _ = application
    passed = await app.call("pro_verify_volume", {"expected_mm3": 1000})
    assert not passed.isError and passed.structuredContent["passed"]
    failed = await app.call("pro_verify_volume", {"expected_mm3": 2000})
    assert failed.isError and not failed.structuredContent["passed"]


async def test_cancelled_request_keeps_cad_lease_until_worker_finishes(application):
    from solidworks_mcp_pro.storage import CadBusyError, CadLease
    app, backend = application
    backend.native_gate = asyncio.Event()
    waiter = asyncio.create_task(app.call("native_wait", {}))
    await backend.native_started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(CadBusyError):
        with CadLease():
            pass
    operation_id = app.registry.list_recent()[0]["operation_id"]
    backend.native_gate.set()
    result = await app.call("pro_operation", {"operation_id": operation_id, "wait_seconds": 1})
    assert not result.isError
    with CadLease():
        pass


async def test_plan_queues_behind_native_call_before_taking_process_lease(application):
    app, backend = application
    backend.native_gate = asyncio.Event()
    native = asyncio.create_task(app.call("native_wait", {}))
    await backend.native_started.wait()
    plan = asyncio.create_task(app.call("pro_run_plan", {
        "request_id": "queued-plan", "dry_run": False,
        "steps": [{"tool": "new_part", "arguments": {}}],
    }))
    # Let both registry and plan task start while native owns the process lease.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    backend.native_gate.set()
    native_result, plan_result = await asyncio.gather(native, plan)
    assert not native_result.isError and not plan_result.isError
    assert [name for name, _ in backend.calls] == ["native_wait", "new_part"]


async def test_large_image_does_not_hide_successful_render_result(application):
    app, _ = application
    path = app.store.allocate(".png")
    # Exercise attachment size handling without allocating a 33 MiB Python blob.
    with path.open("wb") as stream:
        stream.truncate(33 * 1024 * 1024)
    uri = app.store.register(path, "image/png")
    result = app._result({"ok": True, "path": str(path), "image_artifacts": [uri.rsplit("/", 1)[1]]})
    assert not result.isError
    assert result.structuredContent["path"] == str(path)
    assert result.structuredContent["attachment_warnings"]
    assert not any(isinstance(block, types.ImageContent) for block in result.content)


async def test_missing_image_does_not_hide_operation_result(application):
    app, _ = application
    result = app._result({"ok": True, "operation_id": "finished", "image_artifacts": ["missing.png"]})
    assert not result.isError
    assert result.structuredContent["operation_id"] == "finished"
    assert "unavailable" in result.structuredContent["attachment_warnings"][0]


async def test_real_stdio_handshake_discovery_and_offline_tool(tmp_path):
    """Launch the actual installed server; never call native CAD or Visualize."""
    mesh = make_stl(tmp_path / "offline.stl")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "solidworks_mcp_pro", "--workspace", str(tmp_path / "stdio-output")],
        env={**os.environ, "LOCALAPPDATA": str(tmp_path / "localappdata")},
    )

    async def scenario():
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialization = await session.initialize()
                assert initialization.serverInfo.name == "solidworks-mcp-pro"
                catalog = await session.list_tools()
                names = {tool.name for tool in catalog.tools}
                assert "pro_visualize_render" in names and "pro_inspect_stl" in names
                caps = await session.call_tool("pro_capabilities", {})
                assert not caps.isError
                assert caps.structuredContent["native_tool_count"] >= 80
                result = await session.call_tool("pro_inspect_stl", {"path": str(mesh)})
                assert not result.isError and result.structuredContent["triangle_count"] == 1
                guide = await session.read_resource("solidworks-pro://guide")
                assert guide.contents[0].mimeType == "text/markdown"

    await asyncio.wait_for(scenario(), timeout=25)
