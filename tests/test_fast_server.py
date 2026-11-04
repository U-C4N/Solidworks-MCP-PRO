"""Workflow integration, compact discovery and non-mutating result retrieval."""
import asyncio
from copy import deepcopy
import json
import os
import sys

from mcp import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image
import pytest

from solidworks_mcp_pro.fast_tools import COMPACT_TOOLS
from solidworks_mcp_pro.server import ProApplication
from test_server import FakeBackend, read_resource


class WorkflowBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.health_calls = []
        self.color_calls = []
        self.camera_calls = []
        self.pose_calls = []
        self.healthy = True
        self.camera = {"orientation": [1, 0, 0], "scale": 1.0}

    async def health(self, operation, **kwargs):
        self.health_calls.append((operation, deepcopy(kwargs)))
        if operation == "diagnose":
            return {"ok": True, "healthy": self.healthy, "errors": [] if self.healthy else [
                {"code": "MATE_ERROR", "name": "Mate1"}], "warnings": [], "saved": False}
        if operation == "plan":
            return {"ok": True, "plan_id": "rp_test", "requires_apply": True,
                    "steps": [{"action": "rebuild"}], "saved": False}
        return {"ok": True, "plan_id": kwargs["plan_id"], "saved": False}

    async def colorize(self, **kwargs):
        self.color_calls.append(deepcopy(kwargs))
        return {"ok": True, "saved": False, "verified": True, "assignments": kwargs["assignments"]}

    async def camera_state(self, action, **kwargs):
        self.camera_calls.append(action)
        if action == "capture":
            return {"ok": True, "state": deepcopy(self.camera)}
        self.camera = deepcopy(kwargs["state"])
        return {"ok": True, "verified": True}

    async def pose_state(self, action, **kwargs):
        self.pose_calls.append((action, kwargs))
        return {"ok": True, "verified": True}

    async def animation(self, operation, **kwargs):
        self.pose_calls.append((operation, deepcopy(kwargs)))
        if kwargs.get("dry_run"):
            return {"ok": True, "components": [{"component": "Bolt-1", "rigid_unit_scale": True,
                "suppressed": False, "transform": [
                1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]}]}
        return {"ok": True, "verified": True}

    async def visualize(self, operation, **kwargs):
        self.visualize_calls.append((operation, kwargs))
        if operation == "render":
            if self.render_gate is not None:
                self.native_started.set()
                await self.render_gate.wait()
            path = kwargs["output_dir"] / "render.png"
            path.parent.mkdir(parents=True)
            Image.new("RGB", (kwargs["width"], kwargs["height"]), "navy").save(path)
            return {"ok": True, "images": [{"path": str(path)}]}
        return {"ok": True, "verified": True}


@pytest.fixture
async def fast_app(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "locks"))
    backend = WorkflowBackend()
    app = ProApplication(backend, tmp_path / "outputs", tool_profile="compact")
    await app.initialize()
    yield app, backend
    await app.registry.drain()


async def complete(app, result):
    if result.structuredContent.get("operation_status") == "running":
        return await app.call("pro_operation", {
            "operation_id": result.structuredContent["operation_id"], "wait_seconds": 1})
    return result


async def test_compact_catalog_keeps_internal_native_contracts(fast_app):
    app, backend = fast_app
    assert {tool.name for tool in app.tools} == COMPACT_TOOLS
    assert "new_part" in app.schemas
    help_result = await app.call("pro_tool_help", {"tool": "new_part"})
    assert help_result.structuredContent["native_plan_step"]
    result = await app.call("pro_run_plan", {"request_id": "native", "dry_run": False,
        "steps": [{"tool": "new_part", "arguments": {}}]})
    assert not result.isError
    assert backend.calls == [("new_part", {})]
    assert "steps" not in result.structuredContent
    assert result.structuredContent["details_uri"]


async def test_profile_defaults_full_and_rejects_invalid(tmp_path):
    app = ProApplication(FakeBackend(), tmp_path)
    await app.initialize()
    assert app.tool_profile == "full"
    assert len(app.tools) > len(COMPACT_TOOLS)
    with pytest.raises(ValueError, match="tool_profile"):
        ProApplication(FakeBackend(), tmp_path, tool_profile="wrong")


async def test_summary_evidence_is_complete_readable_json(fast_app):
    app, backend = fast_app
    result = await app.call("pro_diagnose", {"document_path": "test.sldprt"})
    assert result.structuredContent["healthy"]
    contents = await read_resource(app, result.structuredContent["details_uri"])
    assert contents.mimeType == "application/json"
    assert json.loads(contents.text)["healthy"]
    before = deepcopy(backend.health_calls)
    full = await app.call("pro_result", {"operation_id": result.structuredContent["operation_id"]})
    assert full.structuredContent["healthy"]
    assert backend.health_calls == before


async def test_evidence_publication_failure_preserves_completed_operation(fast_app, monkeypatch):
    app, _ = fast_app
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(app.store, "register", fail)
    result = await app.call("pro_diagnose", {"document_path": "test.sldprt"})
    assert not result.isError and result.structuredContent["healthy"]
    assert result.structuredContent["operation_id"]
    assert "disk full" in result.structuredContent["evidence_warnings"][0]
    full = await app.call("pro_result", {"operation_id": result.structuredContent["operation_id"]})
    assert full.structuredContent["healthy"]


async def test_image_publication_failure_retains_render_and_cleanup(fast_app, monkeypatch, tmp_path):
    app, _ = fast_app
    model = tmp_path / "model.sldprt"
    model.write_bytes(b"test-model")
    original = app.store.register
    def register(path, mime):
        if mime == "image/png":
            raise OSError("publish unavailable")
        return original(path, mime)
    monkeypatch.setattr(app.store, "register", register)
    done = await complete(app, await app.call("pro_render_job", {
        "document_path": str(model), "request_id": "no-publication", "width": 128, "height": 128}))
    assert not done.isError and done.structuredContent["cleanup"]["camera"]["verified"]
    assert done.structuredContent["artifact_warnings"] and done.structuredContent["image_files"]


async def test_repair_plan_never_applies_and_apply_is_separate_once(fast_app):
    app, backend = fast_app
    planned = await app.call("pro_repair", {"document_path": "test.sldprt"})
    assert planned.structuredContent["requires_apply"]
    assert [action for action, _ in backend.health_calls] == ["plan"]
    for _ in range(2):
        applied = await app.call("pro_repair", {"action": "apply", "plan_id": "rp_test"})
        assert not applied.isError
    assert [action for action, _ in backend.health_calls] == ["plan", "apply"]


@pytest.mark.parametrize("arguments", [
    {"action": "apply"}, {"action": "apply", "plan_id": "rp_test", "document_path": "a"},
    {"plan_id": "rp_test"}, {"document_path": "a", "actions": ["delete_everything"]},
])
async def test_invalid_repair_contract_never_reaches_native(fast_app, arguments):
    app, backend = fast_app
    assert (await app.call("pro_repair", arguments)).isError
    assert backend.health_calls == []


async def test_color_replay_ignores_presentation_and_default_spelling(fast_app):
    app, backend = fast_app
    args = {"document_path": "test.sldprt", "request_id": "color1",
            "assignments": [{"color_rgb": [22, 100, 200]}]}
    first = await app.call("pro_colorize", args)
    second = await app.call("pro_colorize", {**args, "result_view": "full", "dry_run": False})
    assert first.structuredContent["operation_id"] == second.structuredContent["operation_id"]
    assert len(backend.color_calls) == 1
    conflict = await app.call("pro_colorize", {**args, "assignments": [{"color_rgb": [1, 2, 3]}]})
    assert conflict.isError and len(backend.color_calls) == 1


async def test_render_job_one_registration_image_free_polls_and_image_delivery(fast_app, tmp_path):
    app, backend = fast_app
    model = tmp_path / "model.sldprt"
    model.write_bytes(b"test-model")
    args = {"document_path": str(model), "request_id": "render1", "width": 128, "height": 128}
    started = await app.call("pro_render_job", args)
    done = await complete(app, started)
    assert not done.isError, done.structuredContent
    assert done.structuredContent["image_uris"]
    assert not any(isinstance(block, types.ImageContent) for block in done.content)
    replay = await app.call("pro_render_job", {**args, "result_view": "full"})
    assert replay.structuredContent["operation_id"] == done.structuredContent["operation_id"]
    assert len([call for call in backend.visualize_calls if call[0] == "render"]) == 2
    op_id = done.structuredContent["operation_id"]
    again = await app.call("pro_operation", {"operation_id": op_id})
    assert not any(isinstance(block, types.ImageContent) for block in again.content)
    assert again.structuredContent["details_uri"] == done.structuredContent["details_uri"]
    before = len(backend.visualize_calls)
    image = await app.call("pro_result", {"operation_id": op_id, "kind": "image"})
    assert any(isinstance(block, types.ImageContent) for block in image.content)
    assert len(backend.visualize_calls) == before
    assert backend.camera_calls == ["capture", "restore"]


async def test_blocked_render_returns_plan_without_changes(fast_app, tmp_path):
    app, backend = fast_app
    backend.healthy = False
    model = tmp_path / "model.sldprt"
    model.write_bytes(b"test-model")
    done = await complete(app, await app.call("pro_render_job", {
        "document_path": str(model), "request_id": "blocked"}))
    assert done.isError
    assert done.structuredContent["repair_plan"]["plan_id"] == "rp_test"
    assert [action for action, _ in backend.health_calls] == ["diagnose", "plan"]
    assert not backend.visualize_calls and not backend.color_calls


async def test_repair_planning_failure_keeps_original_render_diagnosis(fast_app, tmp_path):
    app, backend = fast_app
    backend.healthy = False
    original = backend.health
    async def health(operation, **kwargs):
        if operation == "plan":
            raise RuntimeError("REPAIR_PRECONDITIONS_INCOMPLETE")
        return await original(operation, **kwargs)
    backend.health = health
    model = tmp_path / "model.sldprt"
    model.write_bytes(b"test-model")
    done = await complete(app, await app.call("pro_render_job", {
        "document_path": str(model), "request_id": "unknown"}))
    assert done.isError and done.structuredContent["status"] == "needs_repair"
    assert done.structuredContent["diagnosis"]["healthy"] is False
    assert done.structuredContent["repair_plan"]["ok"] is False
    assert not backend.visualize_calls


async def test_explicit_images_can_be_requested_with_summary_poll(fast_app, tmp_path):
    app, _ = fast_app
    model = tmp_path / "model.sldprt"
    model.write_bytes(b"test-model")
    done = await complete(app, await app.call("pro_render_job", {
        "document_path": str(model), "request_id": "image-opt-in", "width": 128, "height": 128}))
    image = await app.call("pro_operation", {"operation_id": done.structuredContent["operation_id"],
                                            "result_view": "summary", "include_images": True})
    assert any(isinstance(block, types.ImageContent) for block in image.content)


async def test_actual_stdio_compact_discovery_and_native_schema_without_cad(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "solidworks_mcp_pro",
        "--tool-profile", "compact", "--workspace", str(tmp_path / "stdio")],
        env={**os.environ, "LOCALAPPDATA": str(tmp_path / "locks")})
    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                from solidworks_mcp_pro import __version__
                assert initialized.serverInfo.version == __version__
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == COMPACT_TOOLS
                help_result = await session.call_tool("pro_tool_help", {"tool": "add_box"})
                assert not help_result.isError
                assert "width_mm" in help_result.structuredContent["tool"]["inputSchema"]["properties"]
                capabilities = await session.call_tool("pro_capabilities", {})
                assert capabilities.structuredContent["tool_profile"] == "compact"
    await asyncio.wait_for(scenario(), timeout=25)


async def test_cancelled_waiter_retains_whole_render_job_and_lock(fast_app, tmp_path):
    from solidworks_mcp_pro.storage import CadBusyError, CadLease
    app, backend = fast_app
    model = tmp_path / "model.sldprt"
    model.write_bytes(b"test-model")
    backend.render_gate = asyncio.Event()
    started = await app.call("pro_render_job", {"document_path": str(model), "request_id": "cancel"})
    await backend.native_started.wait()
    waiter = asyncio.create_task(app.call("pro_operation", {
        "operation_id": started.structuredContent["operation_id"], "wait_seconds": 20}))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(CadBusyError):
        with CadLease():
            pass
    backend.render_gate.set()
    done = await complete(app, started)
    assert not done.isError
    assert backend.camera_calls == ["capture", "restore"]


async def test_summary_cannot_retrieve_another_jobs_image(fast_app, tmp_path):
    app, _ = fast_app
    report = await app.call("pro_diagnose", {"document_path": "test.sldprt"})
    denied = await app.call("pro_result", {"operation_id": report.structuredContent["operation_id"],
                                          "kind": "image", "artifact_id": "other.png"})
    assert denied.isError
