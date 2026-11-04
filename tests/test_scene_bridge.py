import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import scene_bridge as bridge


def packed(value):
    return struct.unpack("<d", struct.pack("<ii", value, 0))[0]


class Scene:
    def __init__(self, path):
        self.path = str(path)
        self.BackgroundEnvImage = ""
        self.BackgroundType = 0
        self.EnvironmentRotation = 0.0
        self.FloorShadows = True
        self.FloorReflections = False
        self.RenderingBrightness = 1.0
        self.BackgroundTopGradientColor = 0
        self.BackgroundBottomGradientColor = 0
        self.commits = 0
        self.accept = True
        self.reset_rotation = False

    def GetP2SFileName(self, output):
        assert output == ""
        return self.path

    def Modify(self):
        self.commits += 1
        if self.reset_rotation:
            self.EnvironmentRotation = 0
        return self.accept


class Extension:
    def __init__(self, model):
        self.model = model
        self.ViewDisplayRealView = False
        self.inserts = []
        self.accept = True
        self.render_enabled = {0: True, 1: True, 2: True}
        self.ignore_render_disable = False

    def __call__(self):
        raise AssertionError("COM properties must not be invoked as methods")

    def InsertScene(self, path):
        assert self.ViewDisplayRealView
        self.inserts.append(path)
        self.model.scene.path = path
        return self.accept

    def GetLightEnabledInRender(self, index):
        return self.render_enabled[index]

    def SetLightEnabledInRender(self, index, enabled):
        if not self.ignore_render_disable:
            self.render_enabled[index] = enabled


class Model:
    def __init__(self, path, scene_path):
        self.path = str(path)
        self.scene = Scene(scene_path)
        self.Extension = Extension(self)
        self.read_only = False
        self.view_only = False
        self.doc_type = 2
        self.redraws = 0
        self.writes = []
        self.ignore_disable = False
        self.names = ["Ambient-1", "Directional-1", "Directional-2"]
        self.lights = []
        for light_type in (1, 4, 4):
            values = [0.0] * 19
            values[0] = packed(light_type)
            values[1] = 0.4
            values[2:5] = [1.0, 0.9, 0.8]
            self.lights.append(values)

    def __call__(self):
        raise AssertionError("COM properties must not be invoked as methods")

    def GetPathName(self):
        return self.path

    def GetType(self):
        return self.doc_type

    def IsOpenedReadOnly(self):
        return self.read_only

    def IsOpenedViewOnly(self):
        return self.view_only

    def GetActiveConfiguration(self):
        return SimpleNamespace(GetScene=lambda: self.scene)

    def GetLightSourceCount(self):
        return len(self.lights)

    def GetLightSourceName(self, index):
        return self.names[index]

    def LightSourcePropertyValues(self, index):
        return tuple(self.lights[index])

    def SetLightSourcePropertyValues(self, index, values):
        self.writes.append((index, list(values)))
        if not self.ignore_disable:
            self.lights[index] = list(values)

    def GraphicsRedraw2(self):
        self.redraws += 1


@pytest.fixture
def setup(tmp_path):
    model_path = tmp_path / "render-copy.sldasm"
    scene_path = tmp_path / "softbox.p2s"
    hdr_path = tmp_path / "softbox.hdr"
    for path in (model_path, scene_path, hdr_path):
        path.write_bytes(b"fixture")
    model = Model(model_path, scene_path)
    return SimpleNamespace(ActiveDoc=model), model_path, scene_path, hdr_path


def test_dry_run_reads_native_scene_and_lights_without_mutation(setup):
    app, path, scene, hdr = setup
    result = bridge.apply(app, path, scene, hdri_path=hdr, dry_run=True)
    assert result["ok"] and result["dry_run"]
    assert not result["mutation_attempted"] and not result["saved"]
    assert result["before"]["lights"][1]["name"] == "Directional-1"
    assert result["before"]["lights"][1]["native_type"] == 4
    assert not app.ActiveDoc.Extension.inserts
    assert app.ActiveDoc.scene.commits == 0


def test_inspection_only_default_is_read_only(setup):
    app, path, _, _ = setup
    result = bridge.apply(app, path)
    assert result["ok"] and not result["mutation_attempted"]


def test_insert_hdr_rotation_floor_and_light_readbacks(setup):
    app, path, scene, hdr = setup
    before = app.ActiveDoc.lights[1].copy()
    result = bridge.apply(app, path, scene, hdri_path=hdr, environment_rotation_deg=110,
                          floor_shadows=True, floor_reflections=False,
                          disable_light_names=["Directional-1", "Directional-2"])
    assert result["ok"] and result["verified"]
    assert result["after"]["scene"]["environment_rotation_deg"] == 110
    assert result["after"]["scene"]["background_type"] == 4
    assert result["after"]["scene"]["hdri_path"] == str(hdr)
    assert result["after"]["lights"][0]["disabled"] is False
    assert all(item["disabled"] for item in result["after"]["lights"][1:])
    assert result["realview_restored"] and not app.ActiveDoc.Extension.ViewDisplayRealView
    assert app.ActiveDoc.lights[1][:17] == before[:17]
    assert result["visualize_transfer_verified"] is False


@pytest.mark.parametrize("kwargs", [
    {"environment_rotation_deg": float("nan")}, {"environment_rotation_deg": 361},
    {"environment_rotation_deg": True}, {"floor_shadows": 1}, {"dry_run": None},
    {"floor_shadows": False, "floor_reflections": True},
    {"disable_light_names": ["Directional-1", "Directional-1"]},
    {"disable_light_names": [None]}, {"disable_light_names": "Directional-1"},
    {"scene_path": "relative.p2s"}, {"hdri_path": "https://example.com/studio.hdr"},
])
def test_invalid_arguments_are_rejected_before_native_mutation(setup, kwargs):
    app, path, _, _ = setup
    with pytest.raises(bridge.SceneError, match="INVALID_ARGUMENT"):
        bridge.apply(app, path, **kwargs)
    assert not app.ActiveDoc.Extension.inserts and not app.ActiveDoc.writes


@pytest.mark.parametrize("field", ["read_only", "view_only"])
def test_readonly_documents_refused(setup, field):
    app, path, scene, _ = setup
    setattr(app.ActiveDoc, field, True)
    with pytest.raises(bridge.SceneError, match="DOCUMENT_READ_ONLY"):
        bridge.apply(app, path, scene)
    assert not app.ActiveDoc.Extension.inserts


def test_different_active_document_refused(setup, tmp_path):
    app, path, scene, _ = setup
    other = tmp_path / "other.sldasm"
    other.write_bytes(b"other")
    with pytest.raises(bridge.SceneError, match="DOCUMENT_MISMATCH"):
        bridge.apply(app, other, scene)
    assert not app.ActiveDoc.Extension.inserts


def test_unknown_light_is_preflight_failure(setup):
    app, path, scene, _ = setup
    with pytest.raises(bridge.SceneError, match="LIGHT_NOT_UNIQUE"):
        bridge.apply(app, path, scene, disable_light_names=["not present"])
    assert not app.ActiveDoc.Extension.inserts


def test_duplicate_native_light_names_refused(setup):
    app, path, _, _ = setup
    app.ActiveDoc.names[2] = "Directional-1"
    with pytest.raises(bridge.SceneError, match="LIGHT_NOT_UNIQUE"):
        bridge.apply(app, path, disable_light_names=["Directional-1"])


def test_native_commit_failure_reports_partial_state(setup):
    app, path, _, _ = setup
    app.ActiveDoc.scene.accept = False
    result = bridge.apply(app, path, environment_rotation_deg=90)
    assert not result["ok"] and result["partial_state_possible"]
    assert result["failed"]["stage"] == "commit_scene"
    assert not result["rollback_performed"] and not result["saved"]


def test_readback_mismatch_is_not_success(setup):
    app, path, _, _ = setup
    app.ActiveDoc.scene.reset_rotation = True
    result = bridge.apply(app, path, environment_rotation_deg=90)
    assert not result["ok"]
    assert "readback mismatch" in result["failed"]["error"]


def test_ignored_light_disable_is_not_success(setup):
    app, path, _, _ = setup
    app.ActiveDoc.ignore_disable = True
    result = bridge.apply(app, path, disable_light_names=["Directional-1"])
    assert not result["ok"] and result["partial_state_possible"]
    assert result["disabled_lights"] == []


def test_failed_insert_restores_realview(setup):
    app, path, scene, _ = setup
    app.ActiveDoc.Extension.accept = False
    result = bridge.apply(app, path, scene)
    assert not result["ok"] and result["realview_restored"]
    assert not app.ActiveDoc.Extension.ViewDisplayRealView


def test_missing_reflection_prerequisite_refused_before_mutation(setup):
    app, path, _, _ = setup
    app.ActiveDoc.scene.FloorShadows = False
    with pytest.raises(bridge.SceneError, match="floor_shadows=true"):
        bridge.apply(app, path, floor_reflections=True)


def test_path_property_readback_failure_is_not_unverified_insert_success(setup):
    app, path, scene, _ = setup
    app.ActiveDoc.scene.GetP2SFileName = lambda _: None
    result = bridge.apply(app, path, scene)
    assert not result["ok"]
    assert result["failed"]["stage"] == "insert_scene_readback"


def test_getters_do_not_invoke_callable_dispatch_properties(setup):
    app, path, _, _ = setup
    assert callable(app.ActiveDoc) and callable(app.ActiveDoc.Extension)
    assert bridge.apply(app, path, dry_run=True)["ok"]


def test_modified_other_light_values_fail_preservation_check(setup):
    app, path, _, _ = setup
    original = app.ActiveDoc.SetLightSourcePropertyValues

    def corrupt(index, values):
        original(index, values)
        app.ActiveDoc.lights[index][1] = 0.123

    app.ActiveDoc.SetLightSourcePropertyValues = corrupt
    result = bridge.apply(app, path, disable_light_names=["Directional-1"])
    assert not result["ok"]
    assert "unexpectedly changed" in result["failed"]["error"]


def test_part_files_supported(setup, tmp_path):
    app, _, _, _ = setup
    part = tmp_path / "render-copy.sldprt"
    part.write_bytes(b"part")
    app.ActiveDoc.path = str(part)
    app.ActiveDoc.doc_type = 1
    assert bridge.apply(app, part, dry_run=True)["ok"]


def test_existing_realview_true_preserved(setup):
    app, path, scene, _ = setup
    app.ActiveDoc.Extension.ViewDisplayRealView = True
    assert bridge.apply(app, path, scene)["ok"]
    assert app.ActiveDoc.Extension.ViewDisplayRealView


def test_document_path_must_be_existing_nonempty_file(tmp_path):
    path = tmp_path / "empty.sldasm"
    path.touch()
    with pytest.raises(bridge.SceneError, match="INVALID_ARGUMENT"):
        bridge.apply(SimpleNamespace(ActiveDoc=None), Path(path))


def test_ids_allow_duplicate_native_names_and_disable_render_flags(setup):
    app, path, _, _ = setup
    app.ActiveDoc.names[2] = "Directional-1"
    result = bridge.apply(app, path, disable_light_ids=[1, 2])
    assert result["ok"]
    assert all(item["disabled"] and not item["render_enabled"] for item in result["after"]["lights"][1:])
    assert result["after"]["lights"][0]["render_enabled"]


@pytest.mark.parametrize("ids", [[True], [-1], [256], [1, 1], [1.0], "1"])
def test_invalid_ids_fail_preflight(setup, ids):
    app, path, _, _ = setup
    with pytest.raises(bridge.SceneError, match="INVALID_ARGUMENT"):
        bridge.apply(app, path, disable_light_ids=ids)
    assert not app.ActiveDoc.writes


def test_ids_may_not_be_invalidated_by_scene_insert_in_same_call(setup):
    app, path, scene, _ = setup
    with pytest.raises(bridge.SceneError, match="Insert the scene first"):
        bridge.apply(app, path, scene, disable_light_ids=[1])
    assert not app.ActiveDoc.Extension.inserts


def test_missing_id_is_preflight_failure(setup):
    app, path, _, _ = setup
    with pytest.raises(bridge.SceneError, match="LIGHT_ID_UNKNOWN"):
        bridge.apply(app, path, disable_light_ids=[8])


def test_render_disable_readback_is_required(setup):
    app, path, _, _ = setup
    app.ActiveDoc.Extension.ignore_render_disable = True
    result = bridge.apply(app, path, disable_light_ids=[1])
    assert not result["ok"]
    assert "native render scene" in result["failed"]["error"]


def test_clear_reflections_before_setting_shadows_for_coupled_native_flags(setup):
    app, path, _, _ = setup

    class CoupledScene(Scene):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "FloorReflections" and value is False:
                super().__setattr__("FloorShadows", False)

    app.ActiveDoc.scene = CoupledScene(app.ActiveDoc.scene.path)
    result = bridge.apply(app, path, floor_shadows=True, floor_reflections=False)
    assert result["ok"]
    assert result["after"]["scene"]["floor_shadows"] is True
    assert result["after"]["scene"]["floor_reflections"] is False


def test_com_light_array_is_explicit_safearray_r8(monkeypatch):
    calls = []
    values = [float(index) for index in range(19)]
    variant = object()

    def make_variant(vt, payload):
        assert vt == 0x2000 | 5
        assert payload == values
        assert type(payload) is list
        return variant

    client = SimpleNamespace(VARIANT=make_variant)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(VT_ARRAY=0x2000, VT_R8=5))
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    model = SimpleNamespace(_oleobj_=object(), SetLightSourcePropertyValues=lambda *args: calls.append(args))
    bridge._set_light_values(model, 2, values)
    assert calls == [(2, variant)]


def test_light_array_mock_path_uses_independent_list():
    calls = []
    values = [0.0] * 19
    model = SimpleNamespace(SetLightSourcePropertyValues=lambda *args: calls.append(args))
    bridge._set_light_values(model, 1, values)
    assert calls == [(1, values)]
    assert calls[0][1] is not values


def test_native_brightness_has_bounded_readback_without_transfer_claim(setup):
    app, path, _, _ = setup
    result = bridge.apply(app, path, rendering_brightness=1.8)
    assert result["ok"]
    assert result["after"]["scene"]["rendering_brightness"] == 1.8
    assert result["visualize_transfer_verified"] is False
    assert "requires_render_comparison" in result["rendering_brightness_scope"]


@pytest.mark.parametrize("brightness", [0, 0.09, 5.01, float("inf"), float("nan"), True, "2"])
def test_invalid_brightness_before_native_mutation(setup, brightness):
    app, path, _, _ = setup
    with pytest.raises(bridge.SceneError, match="rendering_brightness"):
        bridge.apply(app, path, rendering_brightness=brightness)
    assert app.ActiveDoc.scene.commits == 0


def test_hdr_set_in_environment_before_plain_backdrop_and_rgb(setup):
    app, path, _, hdr = setup

    class EnvRequiredScene(Scene):
        def __setattr__(self, name, value):
            if name == "BackgroundEnvImage" and value:
                assert self.BackgroundType == 4
            if name == "BackgroundTopGradientColor" and value:
                assert self.BackgroundType == 1
            super().__setattr__(name, value)

    app.ActiveDoc.scene = EnvRequiredScene(app.ActiveDoc.scene.path)
    result = bridge.apply(app, path, hdri_path=hdr, background_type="plain",
                          background_top_rgb=[220, 222, 226])
    assert result["ok"]
    assert result["after"]["scene"]["background_type"] == 1
    assert result["after"]["scene"]["background_type_name"] == "plain"
    assert result["after"]["scene"]["background_top_rgb"] == [220, 222, 226]
    assert result["after"]["scene"]["hdri_path"] == str(hdr)
    assert app.ActiveDoc.scene.BackgroundTopGradientColor == 220 | 222 << 8 | 226 << 16


def test_gradient_colors_readback_both_ends(setup):
    app, path, _, _ = setup
    result = bridge.apply(app, path, background_type="gradient", background_top_rgb=[1, 2, 3],
                          background_bottom_rgb=[251, 252, 253])
    assert result["ok"]
    assert result["after"]["scene"]["background_type"] == 2
    assert result["after"]["scene"]["background_top_rgb"] == [1, 2, 3]
    assert result["after"]["scene"]["background_bottom_rgb"] == [251, 252, 253]


@pytest.mark.parametrize("kwargs", [
    {"background_type": 1}, {"background_type": "image"},
    {"background_type": "plain", "background_top_rgb": [0, 0, 256]},
    {"background_type": "plain", "background_top_rgb": [0, 0, True]},
    {"background_type": "plain", "background_top_rgb": [1, 2]},
    {"background_type": "plain", "background_bottom_rgb": [1, 2, 3]},
    {"background_type": "environment", "background_top_rgb": [1, 2, 3]},
    {"background_type": "environment", "background_bottom_rgb": [1, 2, 3]},
])
def test_invalid_backdrop_values_or_mode_relationship_fail_preflight(setup, kwargs):
    app, path, _, _ = setup
    with pytest.raises(bridge.SceneError, match="INVALID_ARGUMENT"):
        bridge.apply(app, path, **kwargs)
    assert app.ActiveDoc.scene.commits == 0


def test_color_only_uses_current_gradient_mode(setup):
    app, path, _, _ = setup
    app.ActiveDoc.scene.BackgroundType = 2
    result = bridge.apply(app, path, background_bottom_rgb=[11, 12, 13])
    assert result["ok"]


def test_hdr_with_color_requires_explicit_backdrop_override(setup):
    app, path, _, hdr = setup
    with pytest.raises(bridge.SceneError, match="background_top_rgb"):
        bridge.apply(app, path, hdri_path=hdr, background_top_rgb=[11, 12, 13])
    assert app.ActiveDoc.scene.commits == 0


def test_scene_import_colors_require_explicit_mode_before_import(setup):
    app, path, scene, _ = setup
    app.ActiveDoc.scene.BackgroundType = 2
    with pytest.raises(bridge.SceneError, match="explicit background_type"):
        bridge.apply(app, path, scene, background_top_rgb=[11, 12, 13])
    assert not app.ActiveDoc.Extension.inserts


def test_backdrop_only_change_verifies_existing_hdr_unchanged(setup):
    app, path, _, hdr = setup
    app.ActiveDoc.scene.BackgroundEnvImage = str(hdr)
    result = bridge.apply(app, path, background_type="plain", background_top_rgb=[220, 220, 220])
    assert result["ok"]
    assert result["after"]["scene"]["hdri_path"] == str(hdr)


def test_backdrop_change_that_loses_hdr_is_failure(setup):
    app, path, _, hdr = setup

    class LosesHdrScene(Scene):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "BackgroundType" and value == 1:
                super().__setattr__("BackgroundEnvImage", "")

    app.ActiveDoc.scene = LosesHdrScene(app.ActiveDoc.scene.path)
    app.ActiveDoc.scene.BackgroundEnvImage = str(hdr)
    result = bridge.apply(app, path, background_type="plain", background_top_rgb=[220, 220, 220])
    assert not result["ok"]
    assert "preserve the existing HDRI" in result["failed"]["error"]


def test_native_brightness_ignored_is_not_success(setup):
    app, path, _, _ = setup
    original = app.ActiveDoc.scene.Modify

    def ignored():
        app.ActiveDoc.scene.RenderingBrightness = 1.0
        return original()

    app.ActiveDoc.scene.Modify = ignored
    result = bridge.apply(app, path, rendering_brightness=2)
    assert not result["ok"]
    assert "rendering_brightness" in result["failed"]["error"]


def test_named_light_can_gain_new_id_when_scene_is_inserted(setup):
    app, path, scene, _ = setup
    model = app.ActiveDoc
    original_insert = model.Extension.InsertScene
    original_target_values = model.lights[1].copy()

    def replace_lights(scene_path):
        accepted = original_insert(scene_path)
        # Replacing a scene may rebuild its light inventory. The exact name is
        # retained but its new ID3 did not exist in the previous 0..2 inventory.
        model.names = ["New ambient", "New fill", "New rim", "Directional-1"]
        model.lights = [values.copy() for values in model.lights] + [original_target_values.copy()]
        model.Extension.render_enabled = {0: True, 1: True, 2: True, 3: True}
        return accepted

    model.Extension.InsertScene = replace_lights
    result = bridge.apply(app, path, scene, disable_light_names=["Directional-1"])
    assert result["ok"]
    assert result["disabled_lights"] == [{"id": 3, "name": "Directional-1", "verified": True}]
    assert result["after"]["lights"][3]["disabled"] is True
    assert result["after"]["lights"][3]["render_enabled"] is False
    assert all(not item["disabled"] for item in result["after"]["lights"][:3])
