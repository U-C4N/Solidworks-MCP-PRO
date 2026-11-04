"""Native shot-pack orchestration using only synthetic image/backend doubles."""
import asyncio
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from itertools import product

from jsonschema import Draft202012Validator, ValidationError
from PIL import Image
import pytest

from solidworks_mcp_pro.shot_pack import preview_dimensions, project_fit_camera, run_shot_pack, shot_tools
from solidworks_mcp_pro.storage import ArtifactStore


class Backend:
    def __init__(self):
        self.calls = []
        self.camera = {"orientation": "original", "zoom": 1}
        self.original_camera = deepcopy(self.camera)
        self.healthy = True
        self.fail_at = None
        self.invalid_preview = False
        self.render_started = asyncio.Event()
        self.render_gate = None
        self.restore_started = asyncio.Event()
        self.restore_gate = None
        self.bounds = {"min_mm": [0, 0, 0], "max_mm": [100, 70, 10], "size_mm": [100, 70, 10]}

    def record(self, name, kwargs):
        self.calls.append((name, deepcopy(kwargs)))
        if name == self.fail_at:
            return {"ok": False, "verified": False, "code": "INJECTED_FAILURE", "error": name}
        return None

    async def health(self, action, **kwargs):
        return self.record(action, kwargs) or {"ok": True, "healthy": self.healthy,
            "errors": [] if self.healthy else [{"code": "REBUILD_ERROR"}]}

    async def camera_state(self, action, **kwargs):
        failure = self.record("camera_" + action, kwargs)
        if action == "capture":
            return failure or {"ok": True, "state": deepcopy(self.camera)}
        self.restore_started.set()
        if self.restore_gate:
            await self.restore_gate.wait()
        if failure:
            return failure
        self.camera = deepcopy(kwargs["state"])
        return {"ok": True, "verified": True, "state": deepcopy(self.camera)}

    async def design_validate(self, **kwargs):
        return self.record("design_validate", kwargs) or {"ok": True, "passed": True, "state_unchanged": True,
            "measurements": {"bounding_box_mm": deepcopy(self.bounds)}}

    async def visualize(self, operation, **kwargs):
        name = kwargs["job_name"] if operation == "render" else (
            Path(kwargs["output_directory"]).name if operation == "render_project" else operation)
        failure = self.record(name, kwargs)
        if operation == "configure_camera":
            # Simulate partial native mutation, even for a reported failure.
            self.camera = {"orientation": kwargs["view"], "zoom": kwargs.get("zoom_factor", 1)}
        if failure:
            return failure
        if operation == "export_project":
            path = Path(kwargs["path"])
            path.write_bytes(b"native project export double")
            return {"ok": True, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if operation in {"render", "render_project"}:
            self.render_started.set()
            if self.render_gate:
                await self.render_gate.wait()
            folder = Path(kwargs["output_dir"] if operation == "render" else kwargs["output_directory"])
            folder.mkdir()
            size = kwargs["width"], kwargs["height"]
            if self.invalid_preview and "preview" in name:
                size = 64, 64
            image = folder / "native.png"
            Image.new("RGB", size, "navy").save(image)
            # The orchestrator must decode actual output instead of trusting this.
            return {"ok": True, "images": [{"path": str(image), "width": kwargs["width"],
                                            "height": kwargs["height"]}], "renderer": {"native_execution": True}}
        return {"ok": True, "available": True}


@pytest.fixture
def setup(tmp_path):
    model = tmp_path / "Assembly.sldasm"
    model.write_bytes(b"saved native model double")
    return Backend(), ArtifactStore(tmp_path / "outputs"), model


def names(backend):
    return [name for name, _ in backend.calls]


async def test_multiple_shots_generate_real_artifacts_and_restore_one_checkpoint(setup):
    backend, store, model = setup
    original_file = model.read_bytes()
    result = await run_shot_pack(backend, store, document_path=model, shots=[
        {"name": "hero", "view": "iso", "width": 128, "height": 96},
        {"name": "detail", "view": "front", "width": 64, "height": 128}])
    assert result["ok"] and result["status"] == "completed"
    assert result["counts"] == {"requested_shots": 2, "completed_shots": 2,
                                "verified_previews": 2, "verified_finals": 2}
    assert names(backend)[:3] == ["diagnose", "status", "camera_capture"]
    assert names(backend).count("camera_capture") == 1 and names(backend)[-1] == "camera_restore"
    assert backend.camera == backend.original_camera and result["cleanup"]["camera"]["verified"]
    assert model.read_bytes() == original_file and not result["saved"] and not result["rebuilt"]
    assert len(result["images"]) == 3 and result["images"][-1]["role"] == "contact_sheet"
    contact = result["contact_sheet"]
    with Image.open(contact["path"]) as image:
        assert image.size == (contact["width"], contact["height"])
        image.verify()
    manifest_path = Path(result["manifest"]["path"])
    manifest = json.loads(manifest_path.read_text())
    assert manifest["ok"] and manifest["cleanup"]["camera"]["verified"]
    assert result["manifest"]["sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert result["manifest"]["uri"].startswith("solidworks-pro://artifact/")


@pytest.mark.parametrize("aspect,final_size,preview_size", [
    ("square", (1024, 1024), (640, 640)),
    ("landscape", (1920, 1080), (640, 360)),
    ("portrait", (1080, 1920), (360, 640)),
])
async def test_preview_has_same_aspect_as_final_for_each_preset(setup, aspect, final_size, preview_size):
    backend, store, model = setup
    result = await run_shot_pack(backend, store, document_path=model,
                                shots=[{"name": "hero", "view": "front", "aspect": aspect}], passes=40)
    assert result["ok"]
    preview = next(kwargs for name, kwargs in backend.calls if name.endswith("-preview"))
    final = next(kwargs for name, kwargs in backend.calls if name.endswith("-final"))
    assert (preview["width"], preview["height"]) == preview_size
    assert (final["width"], final["height"]) == final_size
    assert preview["passes"] == 25 and final["passes"] == 40
    assert preview["width"] * final["height"] == preview["height"] * final["width"]


@pytest.mark.parametrize("size", [(64, 4096), (4096, 64), (4095, 4096), (1280, 720), (67, 131)])
def test_arbitrary_previews_preserve_exact_ratio_within_native_limits(size):
    width, height = preview_dimensions(*size)
    assert 64 <= width <= size[0] and 64 <= height <= size[1]
    assert width * size[1] == height * size[0]


async def test_custom_camera_is_prevalidated_and_sent_to_native_bridge(setup):
    backend, store, model = setup
    shot = {"name": "custom", "view": "custom", "from_direction": [1, .6, 1.4],
            "up_direction": [0, 1, 0], "zoom_factor": 1.2, "width": 128, "height": 96}
    result = await run_shot_pack(backend, store, document_path=model, shots=[shot])
    assert result["ok"]
    kwargs = next(kwargs for name, kwargs in backend.calls if name == "configure_camera")
    assert kwargs["from_direction"] == shot["from_direction"]
    assert kwargs["up_direction"] == [0, 1, 0] and kwargs["zoom_factor"] == 1.2
    assert kwargs["_exact_active"] is True and kwargs["document_path"] == str(model)


async def test_current_view_after_a_prior_shot_means_original_checkpoint(setup):
    backend, store, model = setup
    result = await run_shot_pack(backend, store, document_path=model, shots=[
        {"name": "first", "view": "front", "width": 64, "height": 64},
        {"name": "original", "view": "current", "width": 64, "height": 64}])
    assert result["ok"] and names(backend).count("camera_capture") == 1
    assert names(backend).count("camera_restore") == 2
    restore = next(kwargs for name, kwargs in backend.calls if name == "camera_restore")
    assert restore["state"] == backend.original_camera


@pytest.mark.parametrize("shots", [
    [], [{"name": "x", "view": "front"}] * 13,
    [{"name": "Same", "view": "front"}, {"name": "same", "view": "top"}],
    [{"name": "../escape", "view": "front"}], [{"name": "x", "view": "invalid"}],
    [{"name": "x", "view": "front", "width": 128}],
    [{"name": "x", "view": "front", "width": True, "height": 128}],
    [{"name": "x", "view": "front", "aspect": "cinema"}],
    [{"name": "x", "view": "front", "from_direction": [1, 0, 0]}],
    [{"name": "x", "view": "custom", "from_direction": [0, 0, 0]}],
    [{"name": "x", "view": "custom", "from_direction": [0, 1, 0], "up_direction": [0, 1, 0]}],
    [{"name": "x", "view": "front", "zoom_factor": float("nan")}],
    [{"name": "valid", "view": "front"}, {"name": "last", "view": "top", "transforms": []}],
])
async def test_entire_pack_is_validated_before_first_backend_call_or_output(setup, shots):
    backend, store, model = setup
    result = await run_shot_pack(backend, store, document_path=model, shots=shots)
    assert not result["ok"] and result["failed_stage"] == "validate"
    assert not backend.calls and not list(store.root.iterdir())


async def test_unhealthy_model_leaves_camera_and_renderer_untouched(setup):
    backend, store, model = setup
    backend.healthy = False
    result = await run_shot_pack(backend, store, document_path=model, shots=[{"name": "hero", "view": "iso"}])
    assert result["status"] == "needs_repair" and names(backend) == ["diagnose"]
    assert backend.camera == backend.original_camera
    assert json.loads(Path(result["manifest_path"]).read_text())["status"] == "needs_repair"


@pytest.mark.parametrize("failure", ["status", "camera_capture"])
async def test_preflight_failure_never_changes_camera_or_renders(setup, failure):
    backend, store, model = setup
    backend.fail_at = failure
    result = await run_shot_pack(backend, store, document_path=model, shots=[{"name": "hero", "view": "iso"}])
    assert not result["ok"] and "configure_camera" not in names(backend)
    assert not any("-preview" in name for name in names(backend))
    assert backend.camera == backend.original_camera


@pytest.mark.parametrize("failure", ["configure_camera", "s01-hero-preview", "s01-hero-final"])
async def test_native_failure_restores_original_camera(setup, failure):
    backend, store, model = setup
    backend.fail_at = failure
    result = await run_shot_pack(backend, store, document_path=model,
                                shots=[{"name": "hero", "view": "top", "width": 128, "height": 96}])
    assert not result["ok"] and result["cleanup"]["camera"]["verified"]
    assert backend.camera == backend.original_camera
    assert names(backend)[-1] == "camera_restore"


async def test_decoded_preview_gate_blocks_final_on_wrong_dimensions(setup):
    backend, store, model = setup
    backend.invalid_preview = True
    result = await run_shot_pack(backend, store, document_path=model,
                                shots=[{"name": "hero", "view": "front", "aspect": "portrait"}])
    assert not result["ok"] and result["code"] == "RENDER_OUTPUT_INVALID"
    assert "s01-hero-final" not in names(backend) and result["counts"]["verified_previews"] == 0
    assert result["cleanup"]["camera"]["verified"]


async def test_failure_on_second_shot_keeps_first_verified_output_and_partial_sheet(setup):
    backend, store, model = setup
    backend.fail_at = "s02-second-preview"
    result = await run_shot_pack(backend, store, document_path=model, shots=[
        {"name": "first", "view": "front", "width": 64, "height": 64},
        {"name": "second", "view": "top", "width": 64, "height": 64}])
    assert not result["ok"] and result["counts"]["verified_finals"] == 1
    assert result["shots"][0]["status"] == "completed" and result["shots"][1]["status"] == "failed"
    assert result["contact_sheet"] and len(result["images"]) == 2
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert not manifest["ok"] and manifest["counts"]["completed_shots"] == 1


async def test_restore_failure_is_indeterminate_in_result_and_manifest(setup):
    backend, store, model = setup
    backend.fail_at = "camera_restore"
    result = await run_shot_pack(backend, store, document_path=model,
                                shots=[{"name": "hero", "view": "top", "width": 64, "height": 64}])
    assert not result["ok"] and result["status"] == "indeterminate"
    assert result["code"] == "RENDER_STATE_RESTORE_FAILED" and not result["cleanup"]["camera"]["verified"]
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["status"] == "indeterminate" and not manifest["ok"]


async def test_cancellation_drains_native_render_and_restores_before_raising(setup):
    backend, store, model = setup
    backend.render_gate = asyncio.Event()
    task = asyncio.create_task(run_shot_pack(backend, store, document_path=model,
        shots=[{"name": "hero", "view": "top", "width": 64, "height": 128}]))
    await backend.render_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and "camera_restore" not in names(backend)
    backend.render_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "s01-hero-final" not in names(backend) and backend.camera == backend.original_camera
    manifest = json.loads(next(store.root.glob("*/manifest.json")).read_text())
    assert manifest["status"] == "indeterminate" and manifest["cleanup"]["camera"]["verified"]


async def test_cancellation_during_camera_cleanup_keeps_barrier(setup):
    backend, store, model = setup
    backend.restore_gate = asyncio.Event()
    task = asyncio.create_task(run_shot_pack(backend, store, document_path=model,
        shots=[{"name": "hero", "view": "top", "width": 64, "height": 64}]))
    await backend.restore_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    backend.restore_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.camera == backend.original_camera
    manifest = json.loads(next(store.root.glob("*/manifest.json")).read_text())
    assert manifest["status"] == "indeterminate" and manifest["cleanup"]["camera"]["verified"]


async def test_progress_failures_are_warnings_and_do_not_repeat_cad_calls(setup):
    backend, store, model = setup
    progress = []

    async def notify(name):
        progress.append(name)
        raise RuntimeError("notification unavailable")

    result = await run_shot_pack(backend, store, document_path=model,
        shots=[{"name": "hero", "view": "front", "width": 64, "height": 64}], progress=notify)
    assert result["ok"] and result["warnings"]
    assert "s01-hero_preview" in progress and "s01-hero_final" in progress
    assert names(backend).count("s01-hero-final") == 1


async def test_existing_output_directory_is_never_rewritten_on_allocation_failure(setup):
    backend, store, model = setup
    existing = store.root / "existing"
    existing.mkdir()
    manifest = existing / "manifest.json"
    manifest.write_text("keep this existing manifest")
    store.allocate = lambda suffix: existing
    result = await run_shot_pack(backend, store, document_path=model,
        shots=[{"name": "hero", "view": "front", "width": 64, "height": 64}])
    assert not result["ok"] and not backend.calls
    assert manifest.read_text() == "keep this existing manifest"


async def test_failed_manifest_publication_retains_honest_failure_in_local_manifest(setup):
    backend, store, model = setup

    def reject(*args):
        raise ValueError("artifact registration unavailable")

    store.register = reject
    result = await run_shot_pack(backend, store, document_path=model,
        shots=[{"name": "hero", "view": "front", "width": 64, "height": 64}])
    assert not result["ok"] and result["code"] == "SHOT_ARTIFACT_FAILED"
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert not manifest["ok"] and manifest["code"] == "SHOT_ARTIFACT_FAILED"
    assert manifest["cleanup"]["camera"]["verified"]


def test_schema_has_explicit_contract_and_rejects_half_dimensions_and_noncustom_vectors():
    tool, = shot_tools()
    Draft202012Validator.check_schema(tool.inputSchema)
    validator = Draft202012Validator(tool.inputSchema)
    arguments = {"document_path": "model.sldprt", "request_id": "shot-1",
                 "shots": [{"name": "hero", "view": "iso", "aspect": "portrait"}]}
    validator.validate(arguments)
    for shot in ({"name": "x", "view": "front", "width": 640},
                 {"name": "x", "view": "front", "from_direction": [1, 0, 0]},
                 {"name": "x", "view": "custom"}):
        with pytest.raises(ValidationError):
            validator.validate({**arguments, "shots": [shot]})


async def test_opt_in_project_pack_exports_once_uses_explicit_camera_and_no_ui_framing(setup):
    backend, store, model = setup
    result = await run_shot_pack(backend, store, document_path=model, render_route="project", shots=[
        {"name": "hero", "view": "iso", "width": 128, "height": 128},
        {"name": "vertical", "view": "custom", "from_direction": [1, .55, 1.4],
         "up_direction": [0, 1, 0], "width": 128, "height": 224}])
    assert result["ok"] and result["render_route"] == "project"
    assert names(backend).count("export_project") == 1 and names(backend).count("design_validate") == 1
    assert "configure_camera" not in names(backend) and backend.camera == backend.original_camera
    assert result["cleanup"]["camera"]["verified"]
    export = next(kwargs for name, kwargs in backend.calls if name == "export_project")
    assert export["document_path"] == str(model) and export["grouping"] == "part"
    for name, kwargs in backend.calls:
        if name.endswith("-preview") or name.endswith("-final"):
            assert kwargs["project_path"] == str(export["path"])
            assert kwargs["camera_target_mm"] == [50, 35, 5] and kwargs["vertical_fov_deg"] == 32
            assert len(kwargs["camera_position_mm"]) == 3
    assert result["counts"]["verified_finals"] == 2
    for image in result["preview_images"] + result["images"][:-1]:
        assert image["render_route"] == "project" and image["execution"] == "native_offline_renderer_project_copy"
    assert json.loads(Path(result["manifest_path"]).read_text())["render_route"] == "project"


@pytest.mark.parametrize("width,height", [(1920, 1080), (1080, 1920), (64, 4096), (4096, 64)])
@pytest.mark.parametrize("direction", [[1, .55, 1.4], [0, 0, 1], [-1, 0, 0], [0, 0, -1]])
def test_project_sphere_fit_contains_every_bbox_corner_in_both_fov_axes(width, height, direction):
    bounds = {"min_mm": [-100, -30, -5], "max_mm": [400, 70, 25]}
    camera = project_fit_camera(bounds, {"view": "custom", "from_direction": direction,
                                        "zoom_factor": 1}, width, height)
    position, target = camera["camera_position_mm"], camera["camera_target_mm"]
    backward = [(a-b) for a, b in zip(position, target)]
    length = math.hypot(*backward)
    backward = [value/length for value in backward]
    right = [backward[2], 0, -backward[0]]
    length = math.hypot(*right)
    right = [value/length for value in right]
    up = [backward[1]*right[2]-backward[2]*right[1],
          backward[2]*right[0]-backward[0]*right[2],
          backward[0]*right[1]-backward[1]*right[0]]
    vertical_tangent = math.tan(math.radians(camera["vertical_fov_deg"]) / 2)
    horizontal_tangent = vertical_tangent * width / height
    for corner in product(*zip(bounds["min_mm"], bounds["max_mm"])):
        offset = [a-b for a, b in zip(corner, position)]
        depth = -sum(a*b for a, b in zip(offset, backward))
        assert depth > 0
        assert abs(sum(a*b for a, b in zip(offset, right))) < depth * horizontal_tangent
        assert abs(sum(a*b for a, b in zip(offset, up))) < depth * vertical_tangent


@pytest.mark.parametrize("shot", [
    {"name": "x", "view": "top"}, {"name": "x", "view": "bottom"},
    {"name": "x", "view": "current"}, {"name": "x", "view": "trimetric"},
    {"name": "x", "view": "custom", "from_direction": [1, 1, 1], "up_direction": [0, 0, 1]},
    {"name": "x", "view": "front", "zoom_factor": 1.5},
])
async def test_project_scope_is_rejected_before_any_native_call_or_export(setup, shot):
    backend, store, model = setup
    result = await run_shot_pack(backend, store, document_path=model, render_route="project", shots=[shot])
    assert not result["ok"] and result["failed_stage"] == "validate" and result["render_route"] == "project"
    assert not backend.calls and not list(store.root.iterdir())


@pytest.mark.parametrize("bounds", [None, {}, {"min_mm": [0, 0, 0], "max_mm": [0, 0, 0]},
    {"min_mm": [0, 0, 0], "max_mm": [-1, 1, 1]},
    {"min_mm": [0, 0, 0], "max_mm": [float("nan"), 1, 1]}])
async def test_invalid_native_bounds_stop_before_project_export(setup, bounds):
    backend, store, model = setup
    backend.bounds = bounds
    result = await run_shot_pack(backend, store, document_path=model, render_route="project",
                                shots=[{"name": "hero", "view": "front"}])
    assert not result["ok"] and "export_project" not in names(backend)
    assert result["cleanup"]["camera"]["verified"]


async def test_project_native_failure_never_falls_back_to_addin(setup):
    backend, store, model = setup
    backend.fail_at = "s01-hero-preview"
    result = await run_shot_pack(backend, store, document_path=model, render_route="project",
                                shots=[{"name": "hero", "view": "front", "width": 128, "height": 128}])
    assert not result["ok"] and result["render_route"] == "project"
    assert "configure_camera" not in names(backend) and "s01-hero-final" not in names(backend)
    assert result["cleanup"]["camera"]["verified"]


def test_project_route_schema_rejects_unsupported_camera_scope():
    tool, = shot_tools()
    validator = Draft202012Validator(tool.inputSchema)
    args = {"document_path": "model.sldprt", "request_id": "project-1", "render_route": "project"}
    validator.validate({**args, "shots": [{"name": "hero", "view": "custom", "from_direction": [1, .5, 1]}]})
    for shot in ({"name": "x", "view": "top"}, {"name": "x", "view": "front", "zoom_factor": 1.1},
                 {"name": "x", "view": "custom", "from_direction": [1, 1, 1], "up_direction": [0, 0, 1]}):
        with pytest.raises(ValidationError):
            validator.validate({**args, "shots": [shot]})
