"""Static render orchestration with no native CAD session or renderer."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from solidworks_mcp_pro.render_job import run_render_job
from solidworks_mcp_pro import render_state_bridge
from solidworks_mcp_pro.storage import ArtifactStore


MATRIX = [0., 0., -1., 0., 1., 0., 1., 0., 0., .123456789123, -.02, .03, 1., 0., 0., 0.]
POSE = {"component": "Handle-1", "position_mm": [200, 30, 50], "rotation_deg": [45, 90, 23]}
COLOR = {"component": "Handle-1", "color_rgb": [15, 130, 210]}


class Backend:
    def __init__(self):
        self.calls = []
        self.healthy = True
        self.fail_at = None
        self.failure_result = None
        self.invalid_preview = False
        self.original_matrix = deepcopy(MATRIX)
        self.matrix = deepcopy(MATRIX)
        self.camera = {"state": "original"}
        self.original_camera = deepcopy(self.camera)
        self.colors = []
        self.render_gate = None
        self.render_started = asyncio.Event()
        self.restore_gate = None
        self.restore_started = asyncio.Event()

    def record(self, name, kwargs):
        self.calls.append((name, deepcopy(kwargs)))
        if name == self.fail_at:
            return self.failure_result or {"ok": False, "code": "INJECTED_FAILURE", "verified": False, "errors": [name]}
        return None

    async def health(self, action, **kwargs):
        failed = self.record(action, kwargs)
        return failed or {"ok": True, "healthy": self.healthy, "errors": [] if self.healthy else [{"code": "REBUILD_ERROR"}]}

    async def colorize(self, **kwargs):
        name = "color_preflight" if kwargs.get("dry_run") else "colorize"
        failed = self.record(name, kwargs)
        if failed:
            return failed
        if not kwargs.get("dry_run"):
            self.colors = deepcopy(kwargs["assignments"])
        return {"ok": True, "saved": False, "dry_run": kwargs.get("dry_run")}

    async def animation(self, action, **kwargs):
        name = "pose_preflight" if kwargs.get("dry_run") else action
        failed = self.record(name, kwargs)
        if not kwargs.get("dry_run"):
            self.matrix[9] = .2  # Simulate a native call changing state before failure.
        if failed:
            return failed
        if kwargs.get("dry_run"):
            return {"ok": True, "read_only": False, "components": [{"component": "Handle-1",
                "transform": deepcopy(self.matrix), "rigid_unit_scale": True, "suppressed": False}]}
        return {"ok": True, "verified": True}

    async def camera_state(self, action, **kwargs):
        name = "camera_capture" if action == "capture" else "restore_camera"
        failed = self.record(name, kwargs)
        if failed:
            return failed
        if action == "capture":
            return {"ok": True, "state": deepcopy(self.camera)}
        self.camera = deepcopy(kwargs["state"])
        return {"ok": True, "verified": True, "saved": False}

    async def pose_state(self, action, **kwargs):
        failed = self.record("restore_poses", kwargs)
        self.restore_started.set()
        if self.restore_gate:
            await self.restore_gate.wait()
        if failed:
            return failed
        self.matrix = deepcopy(kwargs["states"][0]["transform"])
        return {"ok": True, "verified": True, "saved": False, "rebuilt": False}

    async def visualize(self, operation, **kwargs):
        if operation == "status":
            return self.record("renderer_preflight", kwargs) or {"ok": True, "available": True}
        name = kwargs["job_name"] if operation == "render" else "camera"
        failed = self.record(name, kwargs)
        if name == "camera":
            self.camera = {"state": "framed"}
        if failed:
            return failed
        if operation == "render":
            self.render_started.set()
            if self.render_gate:
                await self.render_gate.wait()
            directory = Path(kwargs["output_dir"])
            directory.mkdir()
            size = (kwargs["width"], kwargs["height"])
            if self.invalid_preview and name == "preview":
                size = (64, 64)
            path = directory / "render.png"
            Image.new("RGB", size, "navy").save(path)
            return {"ok": True, "images": [{"path": str(path)}]}
        return {"ok": True, "saved": False}


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "Assembly.sldasm"
    path.write_bytes(b"native fixture")
    return Backend(), ArtifactStore(tmp_path / "output"), str(path)


def names(backend):
    return [name for name, _ in backend.calls]


async def test_normal_job_preflights_colors_then_renders_and_restores_view_without_saving(setup):
    backend, store, path = setup
    result = await run_render_job(backend, store, document_path=path, assignments=[COLOR], width=128, height=96)
    assert result["ok"] and result["status"] == "completed" and not result["saved"]
    assert names(backend) == ["diagnose", "renderer_preflight", "color_preflight", "camera_capture", "colorize", "camera",
                              "preview", "final", "restore_camera"]
    assert backend.colors == [COLOR] and backend.camera == backend.original_camera
    assert result["preview_images"][0]["width"] == 960
    assert result["images"][0]["width"] == 128
    assert result["images"][0]["height"] == 96
    assert len(result["images"][0]["sha256"]) == 64
    assert all(item["duration_ms"] >= 0 for item in result["stages"])
    assert not any("save" in name or "rebuild" in name for name in names(backend))


async def test_exploded_job_restores_exact_raw_matrix_and_camera_after_two_renders(setup):
    backend, store, path = setup
    result = await run_render_job(backend, store, document_path=path, mode="exploded", transforms=[POSE],
                                  assignments=[COLOR], width=64, height=64)
    assert result["ok"] and result["cleanup"]["poses"]["verified"]
    assert backend.matrix == backend.original_matrix and backend.camera == backend.original_camera
    assert names(backend).index("pose_preflight") < names(backend).index("colorize")
    assert names(backend).index("pose") < names(backend).index("camera")
    assert names(backend)[-2:] == ["restore_poses", "restore_camera"]
    restored = next(kwargs for name, kwargs in backend.calls if name == "restore_poses")
    assert restored["states"][0]["transform"] == MATRIX
    assert "rotation_deg" not in restored["states"][0]


async def test_unhealthy_model_returns_needs_repair_without_any_mutations(setup):
    backend, store, path = setup
    backend.healthy = False
    result = await run_render_job(backend, store, document_path=path, assignments=[COLOR])
    assert not result["ok"] and result["status"] == "needs_repair"
    assert result["diagnosis"]["errors"] and names(backend) == ["diagnose"]


async def test_disconnected_scan_is_retained_for_repair_planning_without_other_calls(setup):
    backend, store, path = setup
    backend.fail_at = "diagnose"
    backend.failure_result = {"ok": False, "healthy": False, "errors": [
        {"code": "NOT_CONNECTED", "message": "No attached SOLIDWORKS connection."}], "uninspected": []}
    result = await run_render_job(backend, store, document_path=path, assignments=[COLOR])
    assert not result["ok"] and result["status"] == "needs_repair"
    assert result["diagnosis"] == backend.failure_result
    assert result["stages"][0]["status"] == "failed" and names(backend) == ["diagnose"]


async def test_invalid_non_dict_diagnostic_is_a_failed_contract_not_repair_evidence(setup):
    backend, store, path = setup

    async def invalid_scan(*args, **kwargs):
        backend.record("diagnose", kwargs)
        return ["invalid"]

    backend.health = invalid_scan
    result = await run_render_job(backend, store, document_path=path)
    assert not result["ok"] and result["status"] == "failed"
    assert result["failed_stage"] == "diagnose" and names(backend) == ["diagnose"]


@pytest.mark.parametrize("failure", ["renderer_preflight", "color_preflight", "pose_preflight", "camera_capture"])
async def test_all_preflight_targets_fail_before_color_or_pose_writes(setup, failure):
    backend, store, path = setup
    backend.fail_at = failure
    result = await run_render_job(backend, store, document_path=path, assignments=[COLOR],
                                  mode="exploded", transforms=[POSE])
    assert not result["ok"]
    assert "colorize" not in names(backend) and "pose" not in names(backend)
    assert "preview" not in names(backend)


@pytest.mark.parametrize("failure", ["colorize", "pose", "camera", "preview", "final"])
async def test_stage_failure_restores_every_attempted_temporary_state(setup, failure):
    backend, store, path = setup
    backend.fail_at = failure
    result = await run_render_job(backend, store, document_path=path, assignments=[COLOR],
                                  mode="exploded", transforms=[POSE], width=64, height=64)
    assert not result["ok"] and result["failed_stage"] == failure
    assert backend.matrix == backend.original_matrix and backend.camera == backend.original_camera
    assert "restore_camera" in names(backend)
    if failure != "colorize":
        assert "restore_poses" in names(backend)
    if failure != "final":
        assert "final" not in names(backend)


async def test_invalid_decoded_preview_never_opens_final_render_gate(setup):
    backend, store, path = setup
    backend.invalid_preview = True
    result = await run_render_job(backend, store, document_path=path, mode="exploded", transforms=[POSE])
    assert not result["ok"] and result["code"] == "RENDER_OUTPUT_INVALID"
    assert result["failed_stage"] == "preview" and "final" not in names(backend)
    assert backend.matrix == MATRIX and backend.camera == backend.original_camera
    assert next(stage for stage in result["stages"] if stage["stage"] == "preview")["status"] == "failed"


async def test_failed_pose_restoration_still_attempts_camera_and_reports_indeterminate(setup):
    backend, store, path = setup
    backend.fail_at = "restore_poses"
    result = await run_render_job(backend, store, document_path=path, mode="exploded", transforms=[POSE], width=64, height=64)
    assert not result["ok"] and result["status"] == "indeterminate"
    assert result["code"] == "RENDER_STATE_RESTORE_FAILED" and result["outcome_may_be_indeterminate"]
    assert not result["cleanup"]["poses"]["verified"] and result["cleanup"]["camera"]["verified"]
    assert names(backend)[-1] == "restore_camera"


async def test_failed_color_rollback_preserves_partial_state_evidence_at_top_level(setup):
    backend, store, path = setup
    backend.fail_at = "colorize"
    backend.failure_result = {"ok": False, "code": "COLOR_APPLY_FAILED", "partial_state_possible": True,
        "rollback": {"attempted": True, "verified": False, "errors": ["appearance restore rejected"]}}
    result = await run_render_job(backend, store, document_path=path, assignments=[COLOR])
    assert result["status"] == "indeterminate" and result["partial_state_possible"]
    assert result["outcome_may_be_indeterminate"] and not result["rollback"]["verified"]
    assert result["cleanup"]["camera"]["verified"]


async def test_renderer_option_restore_failure_remains_indeterminate_after_view_cleanup(setup):
    from solidworks_mcp_pro.visualize_bridge import VisualizeError
    backend, store, path = setup
    visualize = backend.visualize

    async def fail_restore(operation, **kwargs):
        if operation == "render":
            raise VisualizeError("RENDER_SETTINGS_RESTORE_FAILED", "Prior native renderer options were rejected.")
        return await visualize(operation, **kwargs)

    backend.visualize = fail_restore
    result = await run_render_job(backend, store, document_path=path)
    assert result["status"] == "indeterminate" and result["outcome_may_be_indeterminate"]
    assert result["code"] == "RENDER_SETTINGS_RESTORE_FAILED" and result["cleanup"]["camera"]["verified"]


async def test_cancellation_waits_for_native_completion_then_restores_before_returning(setup):
    backend, store, path = setup
    backend.render_gate = asyncio.Event()
    task = asyncio.create_task(run_render_job(backend, store, document_path=path, mode="exploded", transforms=[POSE]))
    await backend.render_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and "restore_poses" not in names(backend)
    task.cancel()  # Repeated cancellation must not drop the native completion barrier.
    backend.render_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "final" not in names(backend)
    assert backend.matrix == MATRIX and backend.camera == backend.original_camera


async def test_cancellation_during_cleanup_finishes_both_restore_operations(setup):
    backend, store, path = setup
    backend.restore_gate = asyncio.Event()
    task = asyncio.create_task(run_render_job(backend, store, document_path=path, mode="exploded", transforms=[POSE],
                                               width=64, height=64))
    await backend.restore_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    backend.restore_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.matrix == MATRIX and backend.camera == backend.original_camera
    assert names(backend)[-1] == "restore_camera"


@pytest.mark.parametrize("arguments", [
    {"mode": "exploded"}, {"mode": "normal", "transforms": [POSE]},
    {"mode": "exploded", "transforms": [POSE, POSE]}, {"quality": "fast"},
    {"width": True}, {"height": 0}, {"passes": 0}, {"denoiser": "true"},
    {"assignments": [{"color_rgb": [0, True, 1]}]},
    {"mode": "exploded", "transforms": [{**POSE, "position_mm": [0, float("nan"), 0]}]},
])
async def test_invalid_job_arguments_are_rejected_before_any_backend_calls(setup, arguments):
    backend, store, path = setup
    result = await run_render_job(backend, store, document_path=path, **arguments)
    assert not result["ok"] and result["failed_stage"] == "validate"
    assert not backend.calls


async def test_default_standard_and_4k_settings_are_explicit(setup):
    backend, store, path = setup
    # Stop before rendering: settings are still recorded after pure validation.
    backend.healthy = False
    standard = await run_render_job(backend, store, document_path=path)
    ultra = await run_render_job(backend, store, document_path=path, quality="4k")
    assert standard["settings"]["final"] == {"width": 1920, "height": 1080, "passes": 200, "denoiser": True}
    assert ultra["settings"]["final"] == {"width": 3840, "height": 2160, "passes": 200, "denoiser": True}


class MathData:
    def __init__(self, values):
        self.ArrayData = list(values)


class NativeView:
    def __init__(self):
        self.Orientation3 = MathData(MATRIX)
        self.Translation3 = MathData([.1, .2, .3])
        self.Scale2 = 2.
        self.Camera = None
        self.ObjectSizesAway = 4.
        self.perspective = True

    def HasPerspective(self):
        return self.perspective

    def AddPerspective(self):
        self.perspective = True

    def RemovePerspective(self):
        self.perspective = False


def native_setup(tmp_path):
    path = tmp_path / "Exact.sldasm"
    path.write_bytes(b"fixture")
    components = [SimpleNamespace(Name2=name, Transform2=MathData(MATRIX), IsSuppressed=lambda: False)
                  for name in ("Handle-1", "Base-1")]
    view = NativeView()
    model = SimpleNamespace(ActiveView=view, GetPathName=lambda: str(path), GetType=lambda: 2,
        GraphicsRedraw2=lambda: None, GetComponents=lambda top: components)
    utility = SimpleNamespace(CreateTransform=MathData, CreateVector=MathData)
    app = SimpleNamespace(ActiveDoc=model, GetMathUtility=lambda: utility)
    return app, model, components, path


def test_native_camera_snapshot_and_restore_preserve_raw_view_fields(tmp_path):
    app, model, _, path = native_setup(tmp_path)
    captured = render_state_bridge.camera_state(app, "capture", path)
    model.ActiveView.Orientation3 = MathData([1.] * 16)
    model.ActiveView.Scale2 = .8
    model.ActiveView.perspective = False
    restored = render_state_bridge.camera_state(app, "restore", path, state=captured["state"])
    assert restored["ok"] and restored["verified"]
    assert model.ActiveView.Orientation3.ArrayData == MATRIX
    assert model.ActiveView.Scale2 == 2. and model.ActiveView.perspective
    assert model.ActiveView.ObjectSizesAway == 4.


def test_native_exact_pose_restore_retains_original_matrix_without_euler_conversion(tmp_path):
    app, _, components, path = native_setup(tmp_path)
    states = [{"component": component.Name2, "transform": deepcopy(MATRIX)} for component in components]
    for component in components:
        component.Transform2 = MathData([2.] * 16)
    result = render_state_bridge.restore_poses(app, path, states=states)
    assert result["ok"] and result["verified"] and not result["rebuilt"] and not result["saved"]
    assert all(component.Transform2.ArrayData == MATRIX for component in components)


def test_native_named_camera_is_refused_before_workflow_mutation(tmp_path):
    app, model, _, path = native_setup(tmp_path)
    model.ActiveView.Camera = object()
    with pytest.raises(RuntimeError, match="NAMED_CAMERA_ACTIVE"):
        render_state_bridge.camera_state(app, "capture", path)


def test_native_camera_restore_sets_translation_after_scale_coupling(tmp_path):
    """Native Scale2 changes pan; assigning pan before scale loses exact framing."""
    app, model, _, path = native_setup(tmp_path)

    class CoupledView(NativeView):
        def __init__(self):
            self._scale = 1.
            super().__init__()

        @property
        def Scale2(self):
            return self._scale

        @Scale2.setter
        def Scale2(self, value):
            factor = value / self._scale
            self.Translation3 = MathData([number * factor for number in self.Translation3.ArrayData])
            self._scale = value

    model.ActiveView = CoupledView()
    captured = render_state_bridge.camera_state(app, "capture", path)
    model.ActiveView.Scale2 = .75
    model.ActiveView.Translation3 = MathData([9., 8., 7.])
    restored = render_state_bridge.camera_state(app, "restore", path, state=captured["state"])
    assert restored["ok"] and restored["verified"], restored
    assert restored["state"] == captured["state"]


def test_native_camera_restore_readback_mismatch_has_actionable_error(tmp_path):
    app, model, _, path = native_setup(tmp_path)
    captured = render_state_bridge.camera_state(app, "capture", path)

    def changed_on_redraw():
        model.ActiveView.Translation3 = MathData([9., 8., 7.])

    model.GraphicsRedraw2 = changed_on_redraw
    restored = render_state_bridge.camera_state(app, "restore", path, state=captured["state"])
    assert not restored["ok"] and not restored["verified"]
    assert "readback" in restored["errors"][0]
