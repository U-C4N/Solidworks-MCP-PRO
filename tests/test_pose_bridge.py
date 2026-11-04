"""Batch-pose checks with in-memory native doubles; no running CAD required."""
from types import SimpleNamespace
import math
import sys

import pytest

from solidworks_mcp_pro import pose_bridge as bridge


IDENTITY = [1., 0., 0., 0., 1., 0., 0., 0., 1., 0., 0., 0., 1., 0., 0., 0.]


class Transform:
    def __init__(self, values):
        self.ArrayData = list(values)


class Component:
    def __init__(self, name):
        self.Name2 = name
        self._transform = Transform(IDENTITY)
        self.writes = []
        self.suppressed = False
        self.reject_x = None
        self.fail_after_write = False
        self.fail_restore = False

    @property
    def Transform2(self):
        return self._transform

    @Transform2.setter
    def Transform2(self, value):
        self.writes.append(list(value.ArrayData))
        if self.fail_restore and value.ArrayData == IDENTITY:
            raise RuntimeError("restore refused")
        if value.ArrayData[9] == self.reject_x:
            return
        self._transform = Transform(value.ArrayData)
        if self.fail_after_write and len(self.writes) == 1:
            raise RuntimeError("native failure after changing state")

    def IsSuppressed(self):
        return self.suppressed

    def GetBox(self, include_planes, include_sketches):
        assert include_planes is False and include_sketches is False
        p = self._transform.ArrayData[9:12]
        return [*p, *[value + .01 for value in p]]


class Model:
    def __init__(self, path):
        self.path = str(path)
        self.components = [Component("Base-1"), Component("Handle-1")]
        self.redraws = 0
        self.readonly = False
        self.viewonly = False
        self.kind = 2
        self.on_redraw = None

    def GetPathName(self):
        return self.path

    def GetType(self):
        return self.kind

    def IsOpenedReadOnly(self):
        return self.readonly

    def IsOpenedViewOnly(self):
        return self.viewonly

    def GetComponents(self, top_level):
        assert top_level is True
        return self.components

    def GraphicsRedraw2(self):
        self.redraws += 1
        if self.on_redraw:
            self.on_redraw(self)

    def EditRebuild3(self):
        raise AssertionError("A pose must never rebuild mates")

    def Save3(self, *_):
        raise AssertionError("A pose must never save")


class Utility:
    def __init__(self):
        self.created = []
        self.fail_at = None

    def CreateTransform(self, data):
        self.created.append(list(data))
        if len(self.created) == self.fail_at:
            return None
        return Transform(data)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "Animation Copy.sldasm"
    path.write_bytes(b"native-fixture")
    model, utility = Model(path), Utility()
    app = SimpleNamespace(ActiveDoc=model, GetMathUtility=lambda: utility)
    return app, model, utility, path


def request(name="Handle-1", position=None, rotation=None):
    return {"component": name, "position_mm": position or [10., 20., -30.],
            "rotation_deg": rotation or [0., 0., 90.]}


def test_inventory_reads_all_top_level_poses_and_approximate_bounds_without_writes(setup):
    app, model, utility, path = setup
    result = bridge.pose(app, path, [], dry_run=True)
    assert result["ok"] and not result["mutation_attempted"] and not result["saved"] and not result["rebuilt"]
    assert [item["component"] for item in result["components"]] == ["Base-1", "Handle-1"]
    assert result["components"][0]["transform"] == IDENTITY
    assert result["components"][0]["bounds_mm"] == [0, 0, 0, 10, 10, 10]
    assert "approximate" in result["components"][0]["bounds_kind"]
    assert not utility.created and not model.redraws
    assert all(not c.writes for c in model.components)


def test_inventory_reports_suppressed_components_without_mutation(setup):
    app, model, _, path = setup
    model.components[1].suppressed = True
    model.components[1]._transform = None
    state = bridge.pose(app, path, [], dry_run=True)["components"][1]
    assert state["suppressed"] and state["transform"] is None


def test_batch_prepares_all_transforms_then_redraws_once_and_verifies_positions(setup):
    app, model, utility, path = setup
    result = bridge.pose(app, path, [request("Base-1", [-10, 0, 0]), request()])
    assert result["ok"] and result["verified"] and model.redraws == 1
    assert len(utility.created) == 2
    actual = result["after"][1]
    assert actual["position_mm"] == pytest.approx([10, 20, -30])
    assert actual["rotation_deg"] == pytest.approx([0, 0, 90])
    # Native column-major matrix rotates local +X into assembly +Y.
    assert actual["transform"][:3] == pytest.approx([0, 1, 0])
    assert actual["transform"][9:12] == [.01, .02, -.03]
    assert result["before"][0]["transform"] == IDENTITY


@pytest.mark.parametrize("rotation", [[90, 0, 0], [0, 90, 0], [0, -90, 0], [25, 90, 30],
                                      [-35, -90, 70], [25, -40, 60]])
def test_reported_euler_rotation_reproduces_actual_pose(rotation):
    native = bridge.rotation_columns(rotation)
    restored = bridge.rotation_columns(bridge._euler(native))
    assert restored == pytest.approx(native, abs=1e-9)
    assert bridge._rigid([*native, 0, 0, 0, 1, 0, 0, 0])


@pytest.mark.parametrize("requests, dry_run", [
    ([], False), ({}, True), ([request()] * 129, False), ([request(), request()], False),
    ([{"component": "Handle-1", "position_mm": [1, 2, 3]}], False),
    ([{**request(), "scale": 2}], False), ([request("Group-1/Handle-1")], False),
    ([request("")], False), ([request("Handle\x00-1")], False),
    ([request(position=[0, True, 0])], False), ([request(position=[0, float("nan"), 0])], False),
    ([request(position=[0, float("inf"), 0])], False), ([request(position=[1e6 + 1, 0, 0])], False),
    ([request(position=[10 ** 1000, 0, 0])], False), ([request(position=[1, 2])], False),
    ([request(rotation=[0, 3601, 0])], False), ([request(rotation=["0", 0, 0])], False),
    ([request()], 1),
])
def test_invalid_pose_inputs_fail_before_native_mutation(setup, requests, dry_run):
    app, model, utility, path = setup
    with pytest.raises(bridge.PoseError, match="INVALID_ARGUMENT"):
        bridge.pose(app, path, requests, dry_run=dry_run)
    assert not utility.created and not model.redraws and all(not c.writes for c in model.components)


@pytest.mark.parametrize("name", ["Handle", "handle-1", "Missing-1"])
def test_only_exact_native_name_is_accepted_and_all_targets_validate_first(setup, name):
    app, model, utility, path = setup
    with pytest.raises(bridge.PoseError, match="COMPONENT_NOT_FOUND"):
        bridge.pose(app, path, [request("Base-1"), request(name)])
    assert not utility.created and not model.redraws and all(not c.writes for c in model.components)


def test_wrong_active_document_rejected(setup, tmp_path):
    app, model, _, path = setup
    other = tmp_path / "Other.sldasm"
    other.write_bytes(b"other")
    with pytest.raises(bridge.PoseError, match="DOCUMENT_MISMATCH"):
        bridge.pose(app, other, [request()])
    model.path = ""
    with pytest.raises(bridge.PoseError, match="DOCUMENT_MISMATCH"):
        bridge.pose(app, path, [request()])


@pytest.mark.parametrize("attribute", ["readonly", "viewonly"])
def test_read_only_can_be_inspected_but_not_changed(setup, attribute):
    app, model, _, path = setup
    setattr(model, attribute, True)
    assert bridge.pose(app, path, [], dry_run=True)["read_only"]
    with pytest.raises(bridge.PoseError, match="DOCUMENT_READ_ONLY"):
        bridge.pose(app, path, [request()])


def test_suppressed_target_rejected_before_writing_any_other_target(setup):
    app, model, _, path = setup
    model.components[1].suppressed = True
    with pytest.raises(bridge.PoseError, match="COMPONENT_SUPPRESSED"):
        bridge.pose(app, path, [request("Base-1"), request()])
    assert not model.components[0].writes


def test_transform_creation_failure_is_preflight_not_partial_application(setup):
    app, model, utility, path = setup
    utility.fail_at = 2
    with pytest.raises(bridge.PoseError, match="POSE_PREFLIGHT_FAILED"):
        bridge.pose(app, path, [request("Base-1"), request()])
    assert all(not c.writes for c in model.components)


def test_native_ignored_write_restores_every_original_transform(setup):
    app, model, _, path = setup
    model.components[1].reject_x = .01
    result = bridge.pose(app, path, [request("Base-1"), request()])
    assert not result["ok"] and result["code"] == "POSE_APPLY_FAILED"
    assert result["rollback"]["verified"] and not result["outcome_may_be_indeterminate"]
    assert all(c.Transform2.ArrayData == IDENTITY for c in model.components)


def test_mid_write_exception_restores_even_component_that_raised_after_mutating(setup):
    app, model, _, path = setup
    model.components[1].fail_after_write = True
    result = bridge.pose(app, path, [request("Base-1"), request()])
    assert not result["ok"] and result["rollback"]["verified"]
    assert result["failed_stage"] == "apply:Handle-1"
    assert all(c.Transform2.ArrayData == IDENTITY for c in model.components)


def test_post_redraw_pose_coupling_is_detected_and_rollback_checked(setup):
    app, model, _, path = setup

    def override_once(model):
        if model.redraws == 1:
            model.components[0]._transform.ArrayData[9] = 99

    model.on_redraw = override_once
    result = bridge.pose(app, path, [request("Base-1"), request()])
    assert not result["ok"] and result["failed_stage"] == "verify"
    assert result["rollback"]["verified"]


def test_failed_rollback_is_explicit_and_never_claimed_atomic(setup):
    app, model, _, path = setup
    model.components[0].fail_restore = True
    model.components[1].reject_x = .01
    result = bridge.pose(app, path, [request("Base-1"), request()])
    assert not result["ok"] and not result["rollback"]["verified"]
    assert result["outcome_may_be_indeterminate"] and result["rollback"]["errors"]


@pytest.mark.parametrize("index,value", [(12, 2), (0, -1), (0, float("nan"))])
def test_non_rigid_or_nonfinite_native_transforms_are_not_silently_replaced(setup, index, value):
    app, model, _, path = setup
    model.components[1]._transform.ArrayData[index] = value
    with pytest.raises(bridge.PoseError):
        bridge.pose(app, path, [request()])
    assert all(not c.writes for c in model.components)


def test_com_transform_uses_explicit_r8_safearray(monkeypatch):
    token = object()
    calls = []

    def variant(kind, values):
        assert kind == 0x2000 | 5 and values == IDENTITY
        return token

    utility = SimpleNamespace(_oleobj_=object(), CreateTransform=lambda data: calls.append(data) or Transform(IDENTITY))
    client = SimpleNamespace(VARIANT=variant)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(VT_ARRAY=0x2000, VT_R8=5))
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    result = bridge._transform(utility, IDENTITY)
    assert result.ArrayData == IDENTITY and calls == [token]


def test_large_batch_uses_one_redraw_and_preserves_absolute_coordinates(setup):
    app, model, _, path = setup
    model.components = [Component(f"Part-{index}") for index in range(47)]
    result = bridge.pose(app, path, [request(c.Name2, [i, -i, 2 * i], [30, 40, 50])
                                    for i, c in enumerate(model.components)])
    assert result["ok"] and model.redraws == 1
    assert len(result["after"]) == 47
    assert result["after"][-1]["position_mm"] == pytest.approx([46, -46, 92])
    assert all(math.isfinite(v) for item in result["after"] for v in item["transform"])
