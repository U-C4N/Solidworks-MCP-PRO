"""v0.6 MCP boundaries: failure meaning, replay, reports and image ownership."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from mcp import types
from PIL import Image

from solidworks_mcp_pro.fast_tools import COMPACT_TOOLS
from solidworks_mcp_pro.server import ProApplication
from test_fast_server import WorkflowBackend
from test_server import read_resource


ASSERTION = {"id": "depth", "kind": "dimension", "name": "D1@BlockExtrude",
             "unit": "mm", "expected": 10, "abs_tolerance": 0.01}


class ReleaseBackend(WorkflowBackend):
    def __init__(self):
        super().__init__()
        self.design_calls, self.study_calls = [], []

    async def design_validate(self, **kwargs):
        self.design_calls.append(deepcopy(kwargs))
        return {"ok": True, "passed": False, "status": "fail", "counts": {"failed": 1},
                "assertions": [{**ASSERTION, "actual": 9, "passed": False, "status": "fail"}]}

    async def variant_study(self, **kwargs):
        self.study_calls.append(deepcopy(kwargs))
        if kwargs["dry_run"]:
            return {"ok": True, "dry_run": True, "status": "planned", "source_unchanged": True}
        directory = Path(kwargs["output_directory"])
        directory.mkdir()
        csv = directory / "variants.csv"
        csv.write_text("id,mass_kg\nv001,0.1\n", encoding="utf-8")
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps({"source_unchanged": True}), encoding="utf-8")
        return {"ok": True, "dry_run": False, "status": "completed", "source_unchanged": True,
                "active_document_restored": True, "csv_path": str(csv), "manifest_path": str(manifest),
                "variants": [{"id": "v001", "valid": True}], "counts": {"valid": 1},
                "ranked_variants": [{"rank": 1, "id": "v001", "mass_kg": 0.1}]}


@pytest.fixture
async def release_app(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "locks"))
    backend = ReleaseBackend()
    app = ProApplication(backend, tmp_path / "workspace", tool_profile="compact")
    await app.initialize()
    yield app, backend
    await app.registry.drain()


async def finish(app, response):
    while response.structuredContent.get("operation_status") == "running":
        response = await app.call("pro_operation", {"operation_id": response.structuredContent["operation_id"],
                                                  "wait_seconds": 1})
    return response


async def test_engineering_tools_are_discoverable_in_compact(release_app):
    app, backend = release_app
    names = {tool.name for tool in app.tools}
    assert names == COMPACT_TOOLS
    assert {"pro_connection_doctor", "pro_validate_design", "pro_variant_study", "pro_shot_pack"} <= names
    doctor = await app.call("pro_connection_doctor", {})
    assert doctor.structuredContent["tools"]["comparison"] == "match"
    assert doctor.structuredContent["runtime"]["package_version"] == "0.6.0"
    assert not backend.calls and not backend.visualize_calls


async def test_completed_but_failing_requirements_remain_visible(release_app):
    app, backend = release_app
    response = await finish(app, await app.call("pro_validate_design", {
        "document_path": "test.sldprt", "assertions": [ASSERTION]}))
    data = response.structuredContent
    assert not response.isError and data["passed"] is False
    assert data["counts"]["failed"] == 1 and data["assertions"][0]["actual"] == 9
    evidence = await read_resource(app, data["details_uri"])
    assert json.loads(evidence.text)["assertions"][0]["expected"] == 10
    assert len(backend.design_calls) == 1


@pytest.mark.parametrize("assertion", [
    {**ASSERTION, "unit": "kg"}, {**ASSERTION, "min": 1},
    {**ASSERTION, "abs_tolerance": -1}, {**ASSERTION, "expected": float("nan")},
])
async def test_invalid_requirement_never_reaches_native(release_app, assertion):
    app, backend = release_app
    response = await app.call("pro_validate_design", {"document_path": "test.sldprt", "assertions": [assertion]})
    assert response.isError and backend.design_calls == []


async def test_variant_plan_execute_replay_and_reports(release_app):
    app, backend = release_app
    args = {"document_path": "test.sldprt", "request_id": "study",
            "parameters": [{"dimension": "D1@BlockExtrude", "unit": "mm", "values": [6, 8, 10]}]}
    planned = await finish(app, await app.call("pro_variant_study", args))
    assert planned.structuredContent["dry_run"]
    done = await finish(app, await app.call("pro_variant_study", {**args, "dry_run": False}))
    assert done.structuredContent["source_unchanged"] and done.structuredContent["ranked_variants"]
    csv_uri = done.structuredContent["csv_uri"]
    manifest_uri = done.structuredContent["manifest_uri"]
    csv = await read_resource(app, csv_uri)
    assert csv.mimeType == "text/csv" and csv.text.startswith("id,mass_kg")
    assert json.loads((await read_resource(app, manifest_uri)).text)["source_unchanged"]
    replay = await finish(app, await app.call("pro_variant_study", {**args, "dry_run": False, "result_view": "full"}))
    assert replay.structuredContent["operation_id"] == done.structuredContent["operation_id"]
    assert len(backend.study_calls) == 2
    conflict = await app.call("pro_variant_study", {**args, "dry_run": False, "parameters": [
        {"dimension": "D1@BlockExtrude", "unit": "mm", "values": [12]}]})
    assert conflict.isError and len(backend.study_calls) == 2


async def test_shot_pack_publishes_owned_images_once(release_app, tmp_path):
    app, backend = release_app
    model = tmp_path / "part.sldprt"
    model.write_bytes(b"owned-fake-model")
    args = {"document_path": str(model), "request_id": "shots", "shots": [
        {"name": "hero", "view": "iso", "width": 128, "height": 128},
        {"name": "vertical", "view": "front", "width": 128, "height": 192}], "passes": 2}
    done = await finish(app, await app.call("pro_shot_pack", args))
    assert not done.isError, done.structuredContent
    data = done.structuredContent
    assert data["cleanup"]["camera"]["verified"]
    assert data["counts"]["completed_shots"] == 2 and data["manifest_uri"]
    assert not any(isinstance(block, types.ImageContent) for block in done.content)
    image = await app.call("pro_result", {"operation_id": data["operation_id"], "kind": "image"})
    assert any(isinstance(block, types.ImageContent) for block in image.content)
    before = len(backend.visualize_calls)
    replay = await finish(app, await app.call("pro_shot_pack", {**args, "result_view": "full"}))
    assert replay.structuredContent["operation_id"] == data["operation_id"]
    assert len(backend.visualize_calls) == before


async def test_project_shot_route_reaches_native_export_and_renderer(tmp_path, monkeypatch):
    class ProjectBackend(ReleaseBackend):
        async def design_validate(self, **kwargs):
            return {"ok": True, "passed": True, "state_unchanged": True,
                    "measurements": {"bounding_box_mm": {"min_mm": [0, 0, 0], "max_mm": [100, 70, 10]}}}

        async def visualize(self, operation, **kwargs):
            if operation == "export_project":
                self.visualize_calls.append((operation, kwargs))
                Path(kwargs["path"]).write_bytes(b"owned-project-fixture")
                return {"ok": True}
            if operation == "render_project":
                self.visualize_calls.append((operation, kwargs))
                directory = Path(kwargs["output_directory"])
                directory.mkdir()
                Image.new("RGB", (kwargs["width"], kwargs["height"]), "navy").save(directory / "render.png")
                return {"ok": True, "renderer": {"native_execution": True}}
            return await super().visualize(operation, **kwargs)

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "locks"))
    model = tmp_path / "part.sldprt"
    model.write_bytes(b"owned-fake-part")
    backend = ProjectBackend()
    app = ProApplication(backend, tmp_path / "workspace", tool_profile="compact")
    await app.initialize()
    try:
        done = await finish(app, await app.call("pro_shot_pack", {
            "document_path": str(model), "request_id": "project-portrait", "render_route": "project",
            "shots": [{"name": "portrait", "view": "isometric", "width": 128, "height": 192}]}))
        assert not done.isError, done.structuredContent
        assert done.structuredContent["render_route"] == "project"
        names = [name for name, _ in backend.visualize_calls]
        assert names.count("export_project") == 1 and names.count("render_project") == 2
        assert "render" not in names and "configure_camera" not in names
        exported = next(values for name, values in backend.visualize_calls if name == "export_project")
        assert exported["document_path"] == str(model)
        assert done.structuredContent["cleanup"]["camera"]["verified"]
    finally:
        await app.registry.drain()
