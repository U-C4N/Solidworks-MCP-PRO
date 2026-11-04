"""Native camera behavior with in-memory doubles only; no CAD or UI calls."""
import math
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.camera_bridge import CameraError, configure_camera, view_rotation


IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


class MathData:
    def __init__(self, values):
        self.ArrayData = list(values)


class View:
    def __init__(self):
        self.Orientation3 = MathData(IDENTITY)
        self.Translation3 = MathData([0, 0, 0])
        self._scale = 1.0
        self.ObjectSizesAway = 3.0
        self.Camera = None
        self.perspective = False
        self.reject_scale = None

    @property
    def Scale2(self):
        return self._scale

    @Scale2.setter
    def Scale2(self, value):
        if value != self.reject_scale:
            self._scale = value

    def HasPerspective(self):
        return self.perspective

    def AddPerspective(self):
        self.perspective = True

    def RemovePerspective(self):
        self.perspective = False


class Model:
    def __init__(self, path):
        self.path = str(path)
        self.ActiveView = View()
        self.calls = []
        self.redraw_error = False

    def GetPathName(self):
        return self.path

    def GetType(self):
        return 2

    def ShowNamedView2(self, name, identifier):
        self.calls.append(("standard_view", name, identifier))

    def ViewZoomtofit2(self):
        self.calls.append("fit")
        self.ActiveView.Scale2 = 2.0
        self.ActiveView.Translation3 = MathData([1, 2, 3])

    def GraphicsRedraw2(self):
        self.calls.append("redraw")
        if self.redraw_error:
            raise RuntimeError("redraw unavailable")


@pytest.fixture
def scene(tmp_path):
    path = tmp_path / "Saved Assembly.sldasm"
    path.write_bytes(b"fixture")
    model = Model(path)
    utility = SimpleNamespace(CreateTransform=MathData, CreateVector=MathData)
    app = SimpleNamespace(ActiveDoc=model, GetMathUtility=lambda: utility)
    return app, model, path


def test_inspect_changes_nothing(scene):
    app, model, path = scene
    result = configure_camera(app, path)
    assert result["ok"] and not result["mutation_attempted"] and not result["saved"]
    assert result["state"]["orientation"] == IDENTITY
    assert result["state"]["object_sizes_away"] is None
    assert model.calls == []


def test_custom_orientation_fits_then_zooms_and_keeps_perspective(scene):
    app, model, path = scene
    result = configure_camera(app, path, action="apply", view="custom", from_direction=[1, .7, 1],
                              up_direction=[0, 1, 0], zoom_factor=1.1, perspective=True, object_sizes_away=4)
    assert result["ok"] and result["verified"]["custom_orientation"]
    assert result["after"]["scale"] == pytest.approx(2.2)
    assert result["after"]["object_sizes_away"] == 4
    assert result["after"]["perspective"]
    assert result["after"]["translation"] == [1, 2, 3]
    assert result["after"]["orientation"][:9] == pytest.approx(view_rotation([1, .7, 1]))
    assert model.calls == ["fit", "redraw"]


def test_standard_iso_and_orthographic_mode(scene):
    app, model, path = scene
    model.ActiveView.perspective = True
    result = configure_camera(app, path, action="apply", zoom_factor=.8)
    assert result["ok"] and result["after"]["scale"] == pytest.approx(1.6)
    assert not result["after"]["perspective"]
    assert model.calls[0] == ("standard_view", "", 7)


def test_current_without_fit_keeps_orientation_and_translation(scene):
    app, model, path = scene
    model.ActiveView.Translation3 = MathData([4, 5, 6])
    result = configure_camera(app, path, action="apply", view="current", fit=False, zoom_factor=.5)
    assert result["after"]["scale"] == .5
    assert result["after"]["orientation"] == IDENTITY
    assert result["after"]["translation"] == [4, 5, 6]
    assert model.calls == ["redraw"]


@pytest.mark.parametrize("values", [
    {"zoom_factor": 0}, {"zoom_factor": 2.01}, {"zoom_factor": True}, {"zoom_factor": float("nan")},
    {"perspective": 1}, {"fit": "true"}, {"object_sizes_away": float("inf")},
    {"view": []}, {"action": []}, {"view": "custom"},
    {"from_direction": [1, 1, 1]}, {"view": "custom", "from_direction": [0, 0, 0]},
    {"view": "custom", "from_direction": [0, 1, 0]},
    {"view": "custom", "from_direction": [float("nan"), 1, 1]},
])
def test_invalid_parameters_fail_before_native_mutation(scene, values):
    app, model, path = scene
    with pytest.raises(CameraError, match="INVALID_ARGUMENT"):
        configure_camera(app, path, action="apply", **{k: v for k, v in values.items() if k != "action"}) if "action" not in values else configure_camera(app, path, **values)
    assert model.calls == []


def test_wrong_saved_path_never_applies(scene, tmp_path):
    app, model, path = scene
    other = tmp_path / "Other.sldasm"
    other.write_bytes(b"other")
    with pytest.raises(CameraError, match="DOCUMENT_MISMATCH"):
        configure_camera(app, other, action="apply")
    assert model.calls == []
    model.path = ""
    with pytest.raises(CameraError, match="DOCUMENT_MISMATCH"):
        configure_camera(app, path, action="apply")


def test_named_camera_can_be_inspected_but_not_mutated(scene):
    app, model, path = scene
    model.ActiveView.Camera = object()
    assert configure_camera(app, path)["state"]["named_camera_active"]
    with pytest.raises(CameraError, match="NAMED_CAMERA_ACTIVE"):
        configure_camera(app, path, action="apply")
    assert not model.calls


def test_ignored_scale_change_is_failure_with_verified_restoration(scene):
    app, model, path = scene
    model.ActiveView.reject_scale = 2.2
    result = configure_camera(app, path, action="apply", view="custom", from_direction=[1, 1, 1],
                              perspective=True, object_sizes_away=7, zoom_factor=1.1)
    assert not result["ok"] and result["failed_stage"] == "readback"
    assert result["rollback"]["attempted"] and result["rollback"]["verified"]
    assert not result["outcome_may_be_indeterminate"]
    assert model.ActiveView.Scale2 == 1
    assert not model.ActiveView.perspective
    assert model.ActiveView.Orientation3.ArrayData == IDENTITY
    assert model.ActiveView.Translation3.ArrayData == [0, 0, 0]


def test_unverifiable_restore_never_claims_success(scene):
    app, model, path = scene
    model.redraw_error = True
    result = configure_camera(app, path, action="apply")
    assert not result["ok"] and result["outcome_may_be_indeterminate"]
    assert not result["rollback"]["verified"]


def test_custom_rotation_is_orthonormal_and_uses_model_to_viewer_direction():
    assert view_rotation([0, 0, 1]) == tuple(IDENTITY[:9])
    matrix = view_rotation([1, .7, 1], [0, 1, 0])
    axes = [matrix[index::3] for index in range(3)]
    assert axes[2] == pytest.approx([v / math.hypot(1, .7, 1) for v in [1, .7, 1]])
    for index, axis in enumerate(axes):
        assert math.hypot(*axis) == pytest.approx(1)
        for other in axes[index + 1:]:
            assert sum(a * b for a, b in zip(axis, other)) == pytest.approx(0, abs=1e-14)
    cross = (axes[0][1]*axes[1][2]-axes[0][2]*axes[1][1], axes[0][2]*axes[1][0]-axes[0][0]*axes[1][2], axes[0][0]*axes[1][1]-axes[0][1]*axes[1][0])
    assert cross == pytest.approx(axes[2])


def test_missing_document_and_drawing_are_rejected(scene):
    app, model, path = scene
    app.ActiveDoc = None
    with pytest.raises(CameraError, match="NO_ACTIVE_DOCUMENT"):
        configure_camera(app, path)
    app.ActiveDoc = model
    model.GetType = lambda: 3
    with pytest.raises(CameraError, match="MODEL_REQUIRED"):
        configure_camera(app, path)
