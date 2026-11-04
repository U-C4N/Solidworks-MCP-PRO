from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import named_camera_bridge as bridge
from solidworks_mcp_pro.camera_bridge import CameraError


class Point:
    def __init__(self, values):
        self.ArrayData = list(values)


class Camera:
    OPTICS = {"Perspective": True, "FieldOfViewHeight": 0.3, "FieldOfViewDepth": 0.7,
              "FieldOfViewAngle": 0.5, "AspectRatio": 16 / 9}

    def __init__(self, model, name="Camera1", owned=False):
        self.model = model
        self.Name = name
        self.Description = bridge.OWNER if owned else ""
        self.Type = 1
        self.PositionType = 1
        self.position = [0.0, 0.0, 0.8]
        self.TargetPointPosition = Point([0.0, 0.0, 0.0])
        self.RotationRollAngle = 0.0
        self.LockCameraPosition = False
        self.TargetPointBySelection = False
        self.RotationRollBySelection = False
        self.RotationRollFlipDirection = False
        self.ignore_position_once = False

    def __getattr__(self, name):
        if name in self.OPTICS:
            return self.OPTICS[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self.OPTICS:
            raise AssertionError("Documented get-only optics must never be assigned.")
        object.__setattr__(self, name, value)

    @property
    def ID(self):
        return self.model.cameras.index(self)

    def GetPositionCartesian(self):
        return tuple(self.position)

    def SetPositionCartesian(self, *position):
        if self.ignore_position_once:
            self.ignore_position_once = False
            return
        self.position = list(position)

    def Select2(self, append, mark):
        assert append is False and mark == 0
        self.model.selected = self
        return True


class Model:
    def __init__(self, path):
        self.path = path
        self.cameras = []
        self.Extension = self
        self.ActiveView = self
        self.Camera = None
        self.readonly = False
        self.reject_activate = False
        self.reject_delete = False
        self.redraws = 0
        self.selected = None
        self.insertions = 0

    def GetPathName(self):
        return str(self.path)

    def GetType(self):
        return 2

    def IsOpenedReadOnly(self):
        return self.readonly

    def IsOpenedViewOnly(self):
        return False

    def GetCameraCount(self):
        return len(self.cameras)

    def GetCameraById(self, index):
        return self.cameras[index]

    def InsertCamera(self):
        self.insertions += 1
        camera = Camera(self)
        self.cameras.append(camera)
        return camera

    def SetCameraByName(self, name):
        if self.reject_activate:
            return False
        self.Camera = next((c for c in self.cameras if c.Name == name), None)
        return self.Camera is not None

    def ClearSelection2(self, all_selected):
        self.selected = None

    def DeleteSelection2(self, options):
        assert options == 0
        if self.reject_delete:
            return False
        self.cameras.remove(self.selected)
        return True

    def GraphicsRedraw2(self):
        self.redraws += 1


@pytest.fixture
def environment(tmp_path):
    path = tmp_path / "assembly.sldasm"
    path.write_bytes(b"test model")
    model = Model(path)
    app = SimpleNamespace(ActiveDoc=model, GetMathUtility=lambda: SimpleNamespace(CreatePoint=Point))
    return app, model, str(path)


def apply(environment, **kwargs):
    app, _, path = environment
    return bridge.configure(app, path, action="apply", position_mm=[400, 375, 800],
                            target_mm=[-60, 125, 0], **kwargs)


def test_create_exact_native_camera_and_read_only_optics(environment):
    _, model, _ = environment
    result = apply(environment)
    assert result["ok"] and result["created"] and not result["saved"]
    assert result["after"]["position_mm"] == [400, 375, 800]
    assert result["after"]["target_mm"] == [-60, 125, 0]
    assert result["after"]["field_of_view_height_mm"] == 300
    assert result["after"]["aspect_ratio"] == 16 / 9
    assert model.Camera.Name == "MCP_Video" and model.Camera.Description == bridge.OWNER
    assert model.Camera.position == [0.4, 0.375, 0.8]


def test_update_owned_camera_does_not_create_duplicate(environment):
    apply(environment)
    result = apply(environment, roll_deg=30)
    assert result["ok"] and not result["created"]
    assert result["after"]["roll_deg"] == pytest.approx(30)
    assert environment[1].insertions == 1


def test_inspect_readonly_has_no_mutations(environment):
    app, model, path = environment
    model.readonly = True
    result = bridge.configure(app, path)
    assert result["ok"] and result["cameras"] == []
    assert model.insertions == model.redraws == 0


def test_unowned_prefix_collision_refused(environment):
    _, model, _ = environment
    model.cameras.append(Camera(model, "MCP_Video"))
    with pytest.raises(CameraError, match="CAMERA_NOT_OWNED"):
        apply(environment)
    assert model.insertions == model.redraws == 0


def test_writable_exact_document_guard(environment, tmp_path):
    app, model, path = environment
    model.readonly = True
    with pytest.raises(CameraError, match="DOCUMENT_READ_ONLY"):
        apply(environment)
    model.readonly = False
    other = tmp_path / "other.sldasm"
    other.write_bytes(b"other")
    with pytest.raises(CameraError, match="DOCUMENT_MISMATCH"):
        bridge.configure(app, str(other))
    assert model.insertions == 0


@pytest.mark.parametrize("kwargs", [{"name": "UserCamera"}, {"name": "MCP_../bad"},
    {"activate": 1}, {"roll_deg": float("nan")}, {"roll_deg": 3601}, {"action": []}])
def test_invalid_values_fail_before_native_mutation(environment, kwargs):
    app, model, path = environment
    with pytest.raises(CameraError):
        bridge.configure(app, path, **kwargs)
    assert model.insertions == model.redraws == 0


@pytest.mark.parametrize("position,target", [([True, 0, 0], [0, 0, 1]),
    ([0, 0, float("inf")], [0, 0, 1]), ([1e6 + 1, 0, 0], [0, 0, 1]),
    ([0, 0], [0, 0, 1]), ([0, 0, 0], [0, 0, 0]), ([0, 0, 0], [0, 0, 0.001])])
def test_bad_geometry_rejected(environment, position, target):
    app, model, path = environment
    with pytest.raises(CameraError):
        bridge.configure(app, path, action="apply", position_mm=position, target_mm=target)
    assert model.insertions == 0


def test_activation_failure_deletes_new_camera_and_restores_view(environment):
    _, model, _ = environment
    model.reject_activate = True
    result = apply(environment)
    assert not result["ok"] and result["rollback"]["verified"]
    assert model.cameras == [] and model.Camera is None


def test_failed_creation_cleanup_not_claimed_success(environment):
    _, model, _ = environment
    model.reject_activate = model.reject_delete = True
    result = apply(environment)
    assert not result["ok"] and result["outcome_may_be_indeterminate"]
    assert not result["rollback"]["verified"]


def test_ignored_position_detected_and_previous_camera_restored(environment):
    apply(environment)
    app, model, path = environment
    camera = model.Camera
    camera.ignore_position_once = True
    result = bridge.configure(app, path, action="apply", position_mm=[500, 500, 900], target_mm=[0, 0, 0])
    assert not result["ok"] and result["rollback"]["verified"]
    assert camera.position == [0.4, 0.375, 0.8]
    assert camera.TargetPointPosition.ArrayData == [-0.06, 0.125, 0]


def test_deactivate_and_delete_owned_camera(environment):
    apply(environment)
    app, model, path = environment
    result = bridge.configure(app, path, action="deactivate")
    assert result["ok"] and model.Camera is None and len(model.cameras) == 1
    result = bridge.configure(app, path, action="delete")
    assert result["ok"] and result["deleted"] and model.cameras == []
    assert bridge.configure(app, path, action="delete")["already_absent"]


def test_cannot_deactivate_different_camera(environment):
    apply(environment)
    app, model, path = environment
    user = Camera(model, "UserCamera")
    model.cameras.append(user)
    model.Camera = user
    with pytest.raises(CameraError, match="OTHER_CAMERA_ACTIVE"):
        bridge.configure(app, path, action="deactivate")
    assert model.Camera is user
    assert bridge.configure(app, path, action="delete")["ok"]
    assert model.Camera is user and model.cameras == [user]


def test_apply_without_activate_preserves_existing_active_camera(environment):
    _, model, _ = environment
    user = Camera(model, "UserCamera")
    model.cameras.append(user)
    model.Camera = user
    result = apply(environment, activate=False)
    assert result["ok"] and model.Camera is user


def test_unsupported_linked_owned_camera_not_changed(environment):
    apply(environment)
    model = environment[1]
    model.Camera.TargetPointBySelection = True
    with pytest.raises(CameraError, match="CAMERA_STATE_UNSUPPORTED"):
        apply(environment)
    assert model.Camera.TargetPointBySelection is True


def test_explicit_double_math_point_for_native_dispatch(monkeypatch):
    import pythoncom
    import win32com.client
    seen = []
    utility = SimpleNamespace(_oleobj_=object(), CreatePoint=lambda data: seen.append(data) or Point([0, 0, 0]))
    monkeypatch.setattr(bridge, "_typed", lambda obj, interface: obj)
    app = SimpleNamespace(GetMathUtility=lambda: utility)
    bridge._point(app, [400, 375, 800])
    assert isinstance(seen[0], win32com.client.VARIANT)
    assert seen[0].varianttype == pythoncom.VT_ARRAY | pythoncom.VT_R8
    assert list(seen[0].value) == [0.4, 0.375, 0.8]


def test_camera_to_feature_uses_real_query_interface(monkeypatch):
    import pythoncom
    from solidworks_mcp import binding
    calls = []
    feature_cls = SimpleNamespace(CLSID="feature-iid")
    monkeypatch.setattr(binding, "module", lambda: SimpleNamespace(IFeature=feature_cls))
    monkeypatch.setattr(binding, "wrap", lambda obj, cls: (obj, cls))
    ole = SimpleNamespace(QueryInterface=lambda *args: calls.append(args) or "feature-dispatch")
    value = bridge._feature(SimpleNamespace(_oleobj_=ole))
    assert calls == [("feature-iid", pythoncom.IID_IDispatch)]
    assert value == ("feature-dispatch", feature_cls)


def test_native_deactivation_uses_typed_null_dispatch():
    import pythoncom
    import win32com.client
    view = SimpleNamespace(_oleobj_=object())
    bridge._deactivate(view)
    assert isinstance(view.Camera, win32com.client.VARIANT)
    assert view.Camera.varianttype == pythoncom.VT_DISPATCH and view.Camera.value is None


def test_comtypes_variant_dynamic_wrapper_is_unwrapped_before_qi(monkeypatch):
    calls = []
    class NativePointer:
        _iid_ = "dispatch-iid"
        def QueryInterface(self, cls):
            calls.append(cls)
            return "typed-utility"
    wrapper = SimpleNamespace(_comobj=NativePointer())
    monkeypatch.setattr(bridge, "_module", lambda: SimpleNamespace(IMathUtility="utility-interface"))
    assert bridge._typed(wrapper, "IMathUtility") == "typed-utility"
    assert calls == ["utility-interface"]
