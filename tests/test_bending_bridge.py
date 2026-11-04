import math
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import bending_bridge as bridge


def enclosed_area(segments):
    """Independent Green-integral area of the generated exact line/arc loop."""
    integral = 0.0
    for segment in segments:
        x1, y1 = segment["start"]
        x2, y2 = segment["end"]
        if segment["kind"] == "line":
            integral += x1 * y2 - x2 * y1
        else:
            cx, cy = segment["center"]
            radius = math.hypot(x1 - cx, y1 - cy)
            a, b = math.atan2(y1 - cy, x1 - cx), math.atan2(y2 - cy, x2 - cx)
            if segment["direction"] > 0:
                delta = (b - a) % (2 * math.pi)
            else:
                delta = -((a - b) % (2 * math.pi))
            integral += (radius * cx * (math.sin(b) - math.sin(a))
                         + radius * cy * (math.cos(a) - math.cos(b)) + radius**2 * delta)
    return integral / 2


@pytest.mark.parametrize("angle", [0, 0.01, 1, 5, 15, 30, 45, 60, 75, 89, 90])
@pytest.mark.parametrize("bottom", [0, -22])
def test_profile_closed_constant_thickness_volume_for_all_angles(angle, bottom):
    data = bridge.profile(angle, bottom_offset_mm=bottom)
    segments = data["segments"]
    for index, segment in enumerate(segments):
        assert segment["end"] == pytest.approx(segments[(index + 1) % len(segments)]["start"])
        assert math.dist(segment["start"], segment["end"]) > 0
        if segment["kind"] == "arc":
            assert math.dist(segment["center"], segment["start"]) == pytest.approx(math.dist(segment["center"], segment["end"]))
            assert math.dist(segment["center"], segment["start"]) == pytest.approx(2 if index in [5, 7] else 3.5)
    assert enclosed_area(segments) == pytest.approx(135, abs=1e-8)
    assert enclosed_area(segments) * data["depth_mm"] == pytest.approx(data["expected_volume_mm3"])


def test_existing_sheet_dimensions_and_placement_are_reproduced():
    partial = bridge.profile(45)
    full = bridge.profile(90, bottom_offset_mm=-22)
    assert partial["flange_length_mm"] == pytest.approx(24.840155050657017)
    assert full["flange_length_mm"] == pytest.approx(22.680310101314034)
    assert full["extrusion_x_mm"] == [-40, 40]
    coordinates = [point for segment in full["segments"] for point in (segment["start"], segment["end"])]
    assert min(p[0] for p in coordinates) == pytest.approx(-21.5)
    assert max(p[0] for p in coordinates) == pytest.approx(21.5)
    assert min(p[1] for p in coordinates) == pytest.approx(-22)
    assert max(p[1] for p in coordinates) == pytest.approx(4.180310101314034)


@pytest.mark.parametrize("kwargs", [
    {"angle_deg": -1}, {"angle_deg": 91}, {"angle_deg": float("nan")}, {"angle_deg": True},
    {"angle_deg": 0.005}, {"angle_deg": 0, "thickness_mm": 0}, {"angle_deg": 0, "flat_mm": 90},
    {"angle_deg": 0, "inner_radius_mm": 30}, {"angle_deg": 0, "bottom_offset_mm": -1001},
])
def test_invalid_geometry_is_rejected(kwargs):
    with pytest.raises(bridge.BendingError):
        bridge.profile(**kwargs)


class Body:
    def __init__(self, volume=10800, bounds=None):
        self.volume = volume
        self.bounds = bounds or [-40, -22, -21.5, 40, 4.1803101013, 21.5]

    def GetType(self):
        return 0

    def GetMassProperties(self, density):
        assert density == 1000
        return [0, 0, 0, self.volume / 1e9, 1, 1]

    def GetBodyBox(self):
        return [value / 1000 for value in self.bounds]

    def Copy(self):
        return Body(self.volume, list(self.bounds))


class Feature:
    def __init__(self, model, name, kind="Extrusion", body=None):
        self.model, self.Name, self.kind, self.body = model, name, kind, body
        self.Description = ""
        self.suppression = {}
        self.replace_failures = 0
        self.children = []

    def GetTypeName2(self):
        return self.kind

    def GetNextFeature(self):
        index = self.model.features.index(self) + 1
        return self.model.features[index] if index < len(self.model.features) else None

    def IsSuppressed2(self, scope, configs):
        name = self.model.active if scope == 1 else configs[0]
        return [self.suppression.get(name, False)]

    def SetSuppression2(self, state, scope, configs):
        name = self.model.active if scope == 1 else configs[0]
        self.suppression[name] = state == 0
        return True

    def GetChildren(self):
        return self.children

    def IsBase2(self):
        return True

    def SetBody2(self, body, ids):
        assert ids is False
        if self.replace_failures:
            self.replace_failures -= 1
            return False
        self.body = body
        return True

    def Select2(self, append, mark):
        self.model.selected = self
        return True


class Model:
    def __init__(self, path):
        self.path = path
        self.active = "Acik"
        self.configs = {"Acik": SimpleNamespace(Name="Acik", Description="")}
        self.features = [Feature(self, "Flat"), Feature(self, "45"), Feature(self, "90")]
        self.features[1].suppression["Acik"] = True
        self.features[2].suppression["Acik"] = True
        self.readonly = False
        self.ConfigurationManager = SimpleNamespace(AddConfiguration2=self.add_config)
        self.Extension = SimpleNamespace(DeleteSelection2=self.delete)
        self.selected = None
        self.reject_create = False
        self.reject_rebuilds = 0
        self.created = 0

    def add_config(self, name, comment, alternate, options, parent, description, rebuild):
        assert (comment, alternate, options, parent, description, rebuild) == (bridge.OWNER, "", 0, "", bridge.OWNER, True)
        self.configs[name] = SimpleNamespace(Name=name, Description=description)
        for feature in self.features:
            feature.suppression[name] = feature.suppression.get(self.active, False)
        return self.configs[name]

    def GetPathName(self):
        return str(self.path)

    def GetType(self):
        return 1

    def IsOpenedReadOnly(self):
        return self.readonly

    def IsOpenedViewOnly(self):
        return False

    def FirstFeature(self):
        return self.features[0]

    def GetConfigurationNames(self):
        return list(self.configs)

    def GetActiveConfiguration(self):
        return self.configs[self.active]

    def GetConfigurationByName(self, name):
        return self.configs.get(name)

    def ShowConfiguration2(self, name):
        self.active = name
        return name in self.configs

    def DeleteConfiguration2(self, name):
        del self.configs[name]
        return True

    def GetBodies2(self, kind, visible):
        return [feature.body or Body() for feature in self.features if not feature.IsSuppressed2(1, None)[0]]

    def CreateFeatureFromBody3(self, body, reference_surface, options):
        assert reference_surface is False and options == 1
        self.created += 1
        if self.reject_create:
            return None
        made = Feature(self, "Imported1", "BaseBody", body)
        self.features.append(made)
        return made

    def EditRebuild3(self):
        if self.reject_rebuilds:
            self.reject_rebuilds -= 1
            return False
        return True

    def GraphicsRedraw2(self):
        pass

    def ClearSelection2(self, all):
        self.selected = None

    def delete(self, options):
        assert options == 0
        self.features.remove(self.selected)
        return True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "owned_sheet.sldprt"
    path.write_bytes(b"CAD test fixture")
    model = Model(path)
    monkeypatch.setattr(bridge, "_make_body", lambda *_: Body())
    monkeypatch.setattr(bridge, "_array", lambda values, kind="double": list(values))
    return SimpleNamespace(ActiveDoc=model), path


def bend(setup, **kwargs):
    app, path = setup
    return bridge.apply(app, path, angle_deg=kwargs.pop("angle_deg", 90), suppress_features=kwargs.pop("suppress_features", ["Flat", "45", "90"]), **kwargs)


def test_new_configuration_and_imported_body_created_without_saving(setup):
    app, path = setup
    original_bytes = path.read_bytes()
    result = bend(setup)
    assert result["ok"] and result["verified"] and result["configuration_created"] and result["feature_created"]
    assert result["measured"]["volume_mm3"] == pytest.approx(10800)
    assert app.ActiveDoc.active == "MCP_Bend" and not result["saved"]
    feature = app.ActiveDoc.features[-1]
    assert feature.Name == "MCP_BendBody" and feature.Description == bridge.OWNER
    assert feature.suppression["Acik"] is True
    assert path.read_bytes() == original_bytes


def test_second_frame_reuses_same_feature_and_leaves_original_configs_alone(setup):
    app, _ = setup
    assert bend(setup)["ok"]
    first = app.ActiveDoc.features[-1]
    result = bend(setup, angle_deg=45)
    assert result["ok"] and not result["feature_created"] and not result["configuration_created"]
    assert app.ActiveDoc.features[-1] is first and app.ActiveDoc.created == 1
    assert app.ActiveDoc.features[0].suppression.get("Acik", False) is False


@pytest.mark.parametrize("kwargs", [
    {"configuration": "Acik"}, {"feature_name": "Original"}, {"suppress_features": []},
    {"suppress_features": ["Flat", "Flat"]}, {"suppress_features": ["Missing"]},
])
def test_invalid_ownership_or_features_fail_before_mutation(setup, kwargs):
    app, _ = setup
    with pytest.raises(bridge.BendingError):
        bend(setup, **kwargs)
    assert list(app.ActiveDoc.configs) == ["Acik"] and len(app.ActiveDoc.features) == 3


def test_read_only_part_is_rejected(setup):
    setup[0].ActiveDoc.readonly = True
    with pytest.raises(bridge.BendingError, match="READ_ONLY"):
        bend(setup)


def test_exact_file_identity_required(setup, tmp_path):
    other = tmp_path / "another.sldprt"
    other.write_bytes(b"other")
    with pytest.raises(bridge.BendingError, match="DOCUMENT_MISMATCH"):
        bridge.apply(setup[0], other, angle_deg=0, suppress_features=["Flat"])


def test_unmarked_owned_name_is_not_clobbered(setup):
    setup[0].ActiveDoc.configs["MCP_Bend"] = SimpleNamespace(Name="MCP_Bend", Description="user content")
    with pytest.raises(bridge.BendingError, match="CONFIGURATION_NOT_OWNED"):
        bend(setup)


def test_foreign_feature_using_owned_prefix_is_not_replaced(setup):
    setup[0].ActiveDoc.features.append(Feature(setup[0].ActiveDoc, "MCP_BendBody", "Imported"))
    with pytest.raises(bridge.BendingError, match="FEATURE_NOT_OWNED"):
        bend(setup)


def test_temporary_volume_failure_precedes_all_document_mutation(setup, monkeypatch):
    monkeypatch.setattr(bridge, "_make_body", lambda *_: Body(volume=10700))
    with pytest.raises(bridge.BendingError, match="VOLUME_MISMATCH"):
        bend(setup)
    assert list(setup[0].ActiveDoc.configs) == ["Acik"]


def test_creation_failure_restores_original_configuration_and_suppression(setup):
    setup[0].ActiveDoc.reject_create = True
    result = bend(setup)
    assert not result["ok"] and result["rollback"]["verified"]
    assert setup[0].ActiveDoc.active == "Acik" and "MCP_Bend" not in setup[0].ActiveDoc.configs
    assert len(setup[0].ActiveDoc.GetBodies2(0, False)) == 1


def test_configuration_created_before_native_exception_is_discovered_and_removed(setup):
    model = setup[0].ActiveDoc
    original = model.ConfigurationManager.AddConfiguration2

    def fail_after_creation(*args):
        original(*args)
        raise RuntimeError("COM failed after creating configuration")

    model.ConfigurationManager.AddConfiguration2 = fail_after_creation
    result = bend(setup)
    assert not result["ok"] and result["rollback"]["verified"]
    assert model.active == "Acik" and list(model.configs) == ["Acik"]


def test_imported_feature_created_before_exception_is_reported_as_partial(setup):
    model = setup[0].ActiveDoc
    original = model.CreateFeatureFromBody3

    def fail_after_creation(*args):
        original(*args)
        raise RuntimeError("COM failed after creating body")

    model.CreateFeatureFromBody3 = fail_after_creation
    result = bend(setup)
    assert not result["ok"] and result["partial_state_possible"]
    assert any("Unexpected new features" in error for error in result["rollback"]["errors"])


def test_replace_failure_restores_old_body(setup):
    assert bend(setup)["ok"]
    setup[0].ActiveDoc.features[-1].replace_failures = 1
    result = bend(setup, angle_deg=45)
    assert not result["ok"] and result["rollback"]["verified"]
    assert result["failed"]["stage"] == "replace_owned_body"


def test_failed_body_restore_reports_partial_state(setup):
    assert bend(setup)["ok"]
    setup[0].ActiveDoc.features[-1].replace_failures = 2
    result = bend(setup, angle_deg=45)
    assert not result["ok"] and result["partial_state_possible"]
    assert not result["rollback"]["verified"]


def test_extra_solid_not_silently_overwritten(setup):
    setup[0].ActiveDoc.features.append(Feature(setup[0].ActiveDoc, "OtherUserSolid"))
    result = bend(setup)
    assert not result["ok"] and "UNEXPECTED_SOLID" in result["failed"]["error"]
    assert setup[0].ActiveDoc.created == 0 and result["rollback"]["verified"]


def test_existing_bend_feature_active_elsewhere_is_rejected(setup):
    assert bend(setup)["ok"]
    setup[0].ActiveDoc.features[-1].suppression["Acik"] = False
    with pytest.raises(bridge.BendingError, match="FEATURE_SCOPE_INVALID"):
        bend(setup)


def test_dependent_features_are_not_broken_by_body_replacement(setup):
    assert bend(setup)["ok"]
    setup[0].ActiveDoc.features[-1].children = [object()]
    with pytest.raises(bridge.BendingError, match="FEATURE_NOT_OWNED"):
        bend(setup)


def test_native_curve_recipe_uses_metres_explicit_arrays_and_minor_arcs(monkeypatch):
    data = bridge.profile(45)
    calls = []
    trimmed = []

    class Curve:
        def CreateTrimmedCurve2(self, *points):
            assert len(points) == 6 and all(abs(value) <= 0.08 for value in points)
            trimmed.append(self)
            return self

    def arc(center, axis, radius, start, end):
        calls.append((center, axis, radius, start, end))
        assert axis in [(-1, 0, 0), (1, 0, 0)]
        assert radius == pytest.approx(math.dist(start, center))
        return Curve()

    body = Body()
    surface = SimpleNamespace(CreateTrimmedSheet4=lambda curves, preserve: (curves, preserve))
    modeler = SimpleNamespace(CreateLine=lambda *_: Curve(), CreateArc=arc,
                              CreatePlanarSurface2=lambda *_: surface)

    def extrude(sheet, vector, depth):
        assert sheet == (trimmed, True) and vector == (1, 0, 0) and depth == 0.08
        return body

    modeler.CreateExtrudedBody = extrude
    app = SimpleNamespace(GetModeler=lambda: modeler,
                          GetMathUtility=lambda: SimpleNamespace(CreateVector=lambda x: x))
    monkeypatch.setattr(bridge, "_array", lambda values, kind="double": values)
    assert bridge._make_body(app, data) is body
    assert len(trimmed) == 12 and len(calls) == 4
