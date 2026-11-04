"""Independent feature contracts: exact targets, units, rollback and native dispatch."""
from pathlib import Path
from types import SimpleNamespace
import math
import json
import xml.etree.ElementTree as ET

import pytest

from solidworks_mcp_pro.native import features as f
from solidworks_mcp_pro.native.errors import CadError


class Dimension:
    def __init__(self, name="D1@Boss", value=.01, kind=0, driving=2):
        self.name, self._value, self.kind, self.DrivenState = name, value, kind, driving
        self.reject = False
        self.normalize_positive = False

    def GetNameForSelection(self):
        return self.name

    def GetType(self):
        return self.kind

    @property
    def SystemValue(self):
        return self._value

    @SystemValue.setter
    def SystemValue(self, value):
        if not self.reject:
            self._value = abs(value) if self.normalize_positive else value


class Feature:
    def __init__(self, name, model, kind="Boss", dimension=None, specific=None):
        self.Name, self.model, self.kind = name, model, kind
        self.dimension, self.specific, self.children = dimension, specific, []
        self.suppressed = False
        self.error, self.warning = 0, False

    def GetTypeName2(self):
        return self.kind

    def GetNextFeature(self):
        index = self.model.features.index(self)
        return self.model.features[index + 1] if index + 1 < len(self.model.features) else None

    def GetFirstSubFeature(self):
        return None

    def GetErrorCode2(self):
        return self.error, self.warning

    def IsSuppressed(self):
        return self.suppressed

    def SetSuppression2(self, state, config, names):
        self.suppressed = state == 0
        return True

    def GetChildren(self):
        return self.children

    def GetSpecificFeature2(self):
        return self.specific if self.specific is not None else self

    def GetDefinition(self):
        return self.specific

    def GetFirstDisplayDimension(self):
        return SimpleNamespace(GetDimension2=lambda index: self.dimension) if self.dimension else None

    def GetNextDisplayDimension(self, current):
        return None


class Edge:
    def __init__(self, start=(0, 0, 0), end=(.01, 0, 0), center=None, diameter=.01):
        self.start = SimpleNamespace(GetPoint=lambda: start)
        self.end = SimpleNamespace(GetPoint=lambda: end)
        self.center, self.diameter = center, diameter
        self.adjacent = []

    def GetLength(self):
        return math.dist(self.start.GetPoint(), self.end.GetPoint())

    def GetStartVertex(self):
        return self.start

    def GetEndVertex(self):
        return self.end

    def GetCurve(self):
        return SimpleNamespace(IsCircle=lambda: self.center is not None,
                               CircleParams=[*(self.center or (0, 0, 0)), 0, 0, 1, self.diameter / 2])

    def GetTwoAdjacentFaces2(self):
        return self.adjacent


class Body:
    def __init__(self, name):
        self.Name = name
        self.edges = [Edge()]

    def GetEdges(self):
        return self.edges

    def GetMassProperties(self, density):
        return [0, 0, 0, 1e-6, .01, density * 1e-6]

    def GetBodyBox(self):
        return [0, 0, 0, .01, .02, .03]


class Definition(SimpleNamespace):
    def Initialize(self, kind):
        self.initialized = kind
        return True

    def InitializeThreadData(self):
        return True

    def SetFaces(self, index, faces):
        self.faces = getattr(self, "faces", {})
        self.faces[index] = faces


class Manager:
    def __init__(self, model):
        self.model = model
        self.definitions, self.created, self.chamfers, self.combines = [], [], [], []
        self.split = None
        self.reject = False
        self.mirrors = []

    def CreateDefinition(self, kind):
        value = Definition(kind=kind)
        self.definitions.append(value)
        return value

    def CreateFeature(self, definition):
        self.created.append(definition)
        if self.reject:
            return None
        feature = Feature("Created" + str(len(self.created)), self.model)
        self.model.features.append(feature)
        return feature

    def InsertCombineFeature(self, *args):
        self.combines.append(args)
        operation, main, tools = args
        if operation in (15901, 15903) and (main is not None or len(tools) < 2):
            return None
        return self.CreateFeature(Definition(kind="combine"))

    def InsertFeatureChamfer(self, *args):
        self.chamfers.append(args)
        return self.CreateFeature(Definition(kind="chamfer"))

    def PreSplitBody2(self):
        return self.model.bodies

    def PostSplitBody2(self, *args):
        self.split = args
        if self.model.selection:
            return None
        return self.CreateFeature(Definition(kind="split"))

    def InsertMirrorFeature2(self, *args):
        self.mirrors.append((args, self.model.selection[:]))
        if sorted(mark for _, mark in self.model.selection) != [1, 2]:
            return None
        return self.CreateFeature(Definition(kind="mirror"))

    def FeatureFillet3(self, *args):
        self.variable_fillet = args
        return self.CreateFeature(Definition(kind="variable-fillet"))


class Equations:
    def __init__(self):
        self.values = ['"Width" = 10mm']
        self.Status = 0
        self.reject_name = None
        self.added = []

    def GetCount(self):
        return len(self.values)

    def Equation(self, index):
        return self.values[index]

    def Value(self, index):
        self.Status = index
        return .01

    def GlobalVariable(self, index):
        return True

    def SetEquation(self, index, value):
        self.values[index] = value

    def Add2(self, index, equation, solve):
        self.added.append((index, equation, solve))
        if self.reject_name and self.reject_name in equation:
            return -1
        self.values.append(equation)
        return len(self.values) - 1

    def Delete(self, index):
        self.values.pop(index)
        return 0

    def EvaluateAll(self):
        return 0


class Model:
    def __init__(self):
        self.dimension = Dimension()
        self.features = [Feature("Boss", self, dimension=self.dimension), Feature("Hole", self)]
        self.bodies = [Body("Main"), Body("Tool")]
        self.FeatureManager = Manager(self)
        self.Extension = SimpleNamespace(DeleteSelection2=self.delete_selected, ReorderFeature2=self.reorder)
        self.selection = []
        self.rebuild_results = []
        self.equations = Equations()
        self.MaterialPropertyValues = [.7, .7, .7, 1., 1., .5, .3, 0., 0.]
        self.current_material = ("", "")
        self.density = 1000
        self.stamp = 42
        self.delete_refused = False
        self.shell_refused = False
        self.configuration_count = 1
        self.material_configurations = []
        self.material_write_configurations = []

    def GetUpdateStamp(self):
        return self.stamp

    def FirstFeature(self):
        return self.features[0] if self.features else None

    def Parameter(self, name):
        return self.dimension if name == self.dimension.name else None

    def GetBodies2(self, kind, hidden):
        return self.bodies

    def ClearSelection2(self, all):
        self.selection.clear()

    def EditRebuild3(self):
        return self.rebuild_results.pop(0) if self.rebuild_results else True

    def GetEquationMgr(self):
        return self.equations

    def delete_selected(self, flags):
        if self.delete_refused:
            return False
        for entity, _ in self.selection:
            if entity in self.features:
                self.features.remove(entity)
        return True

    def reorder(self, feature, target, location):
        assert isinstance(feature, str) and isinstance(target, str)
        self.reorder_arguments = feature, target, location
        feature = next(item for item in self.features if item.Name == feature)
        target = next(item for item in self.features if item.Name == target)
        self.features.remove(feature)
        self.features.insert(self.features.index(target), feature)
        return True

    def InsertAxis2(self, autosize):
        self.features.append(Feature("Axis", self, "RefAxis"))
        return True

    def FeatureByPositionReverse(self, index):
        return self.features[-1 - index]

    def GetActiveConfiguration(self):
        return SimpleNamespace(Name="Default")

    def GetConfigurationCount(self):
        return self.configuration_count

    def InsertFeatureShell(self, thickness, outward):
        self.shell_arguments = thickness, outward
        self.shell_selection = self.selection[:]
        if not self.shell_refused:
            feature = Feature("Shell1", self, "Shell", specific=Definition(Thickness=thickness))
            self.features.append(feature)

    def GetMaterialPropertyName2(self, config):
        self.material_configurations.append(config)
        if self.configuration_count == 1 and config:
            return "", ""
        return self.current_material

    def SetMaterialPropertyName2(self, config, database, name):
        self.material_write_configurations.append(config)
        self.current_material = name, database
        if database:
            xml = ET.parse(database)
            densities = [e.get("value") for e in xml.iter() if e.tag.endswith("DENS")]
            if densities:
                self.density = float(densities[0])

    def GraphicsRedraw2(self):
        pass


class Session:
    def __init__(self):
        self.model = Model()
        self.planes = {name: Feature(name, self.model, "RefPlane") for name in ("front", "top", "right")}
        self.edge_list = self.model.bodies[0].edges
        self.face = None
        self.databases = []
        self.material_folders = "Original material libraries"
        self.material_folder_writes = []
        self.material_refreshes = 0
        self.app = SimpleNamespace(GetMaterialDatabases=self.refresh_materials,
            GetUserPreferenceStringValue=lambda preference: self.material_folders,
            SetUserPreferenceStringValue=self.set_material_folders)

    def refresh_materials(self):
        self.material_refreshes += 1
        return self.databases

    def set_material_folders(self, preference, value):
        assert preference == 28
        self.material_folder_writes.append(value)
        self.material_folders = value
        return True

    def _ensure(self):
        return self.app

    def _require_model(self):
        return self.model

    def _require_part(self):
        return self.model

    def select(self, value, mark=0, append=False):
        if not append:
            self.model.selection.clear()
        self.model.selection.append((value, mark))
        return True

    def resolve_edges(self, selector):
        return self.edge_list

    def resolve_face(self, ref, point_mm=None):
        return self.face

    def resolve_plane(self, name):
        return self.planes[name]

    def feature_result(self, feature, name=None, **extra):
        if feature is None:
            raise CadError("FEATURE_CREATION_FAILED", "No feature")
        if name:
            feature.Name = name
        return {"ok": True, "feature": feature.Name, **extra}

    def get_mass_properties(self):
        return {"ok": True, "mass_properties": {"volume_mm3": 1000, "density_kg_m3": self.model.density}}


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(f.binding, "wrap", lambda value, interface: value)
    monkeypatch.setattr(f.binding, "array_double", list)
    monkeypatch.setattr(f.binding, "array_dispatch", lambda values, **kwargs: list(values))
    return Session()


def test_all_twenty_four_handlers_are_independent_callables():
    assert len(f.TOOLS) == 24
    assert all(callable(value) and value.__module__ == f.__name__ for value in f.TOOLS.values())
    text = Path(f.__file__).read_text(encoding="utf-8")
    assert "from solidworks_mcp " not in text and "import solidworks_mcp." not in text


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 0, -1, 10**500])
def test_bad_radius_rejected_before_native_calls(session, value):
    with pytest.raises(CadError):
        f.add_fillet(session, value)
    assert not session.model.FeatureManager.created


def test_fillet_native_radius_is_metres_and_uses_explicit_edges(session):
    result = f.add_fillet(session, 2, tangent_propagation=True)
    definition = session.model.FeatureManager.created[0]
    assert definition.DefaultRadius == .002 and definition.PropagateToTangentFaces
    assert result["edge_count"] == 1
    assert session.model.selection == [(session.edge_list[0], 1)]


def test_rejected_native_feature_is_never_success(session):
    session.model.FeatureManager.reject = True
    with pytest.raises(CadError, match="no feature"):
        f.add_fillet(session, 1)


def test_variable_radius_rejects_unmatched_control_before_creation(session):
    with pytest.raises(CadError, match="must match"):
        f.add_fillet(session, 1, radii_at_mm=[[123, 0, 0, 2]])
    assert not session.model.FeatureManager.created


def test_variable_radius_uses_native_endpoint_references(session):
    result = f.add_fillet(session, 1, radii_at_mm=[[10, 0, 0, 2]])
    args = session.model.FeatureManager.variable_fillet
    assert args[4] == 1 and args[11] == [.001, .002]
    assert result["variable_radius"]


@pytest.mark.parametrize("operation,code", [("add", 15903), ("subtract", 15902), ("intersect", 15901)])
def test_body_boolean_uses_resolved_body_objects(session, operation, code):
    f.combine_bodies(session, operation, "Main", ["Tool"])
    native = session.model.FeatureManager.combines[-1]
    if operation == "subtract":
        assert native == (code, session.model.bodies[0], [session.model.bodies[1]])
    else:
        assert native == (code, None, session.model.bodies)


def test_body_tokens_refuse_changed_geometry(session):
    data = f.list_bodies(session)
    session.model.stamp += 1
    with pytest.raises(CadError, match="predates"):
        f.combine_bodies(session, "add", data["bodies"][0]["id"])


def test_body_self_subtraction_rejected(session):
    with pytest.raises(CadError, match="distinct"):
        f.combine_bodies(session, "subtract", "Main", ["Main"])
    assert not session.model.FeatureManager.combines


def test_split_marks_all_bodies_without_creating_external_files(session):
    result = f.split_body(session, "front")
    args = session.model.FeatureManager.split
    assert args[1] is False and args[3] == ["", ""] and args[4] == ""
    assert not result["external_files_created"]


def test_native_split_forces_typed_null_origins_and_string_paths(session, monkeypatch):
    calls = []
    for body in session.model.bodies:
        body._oleobj_ = object()

    def dispatch_array(values, *, force=False):
        calls.append((list(values), force))
        return list(values)

    string_arrays = []
    monkeypatch.setattr(f.binding, "array_dispatch", dispatch_array)
    monkeypatch.setattr(f.binding, "array_string", lambda values: string_arrays.append(list(values)) or list(values))
    assert f.split_body(session, "front")["ok"]
    assert ([None, None], True) in calls
    assert string_arrays == [["", ""]]


def test_native_thread_resolves_axis_center_and_diameter(session):
    session.model.bodies[0].edges = [Edge(center=(0, 0, .02), diameter=.01)]
    session.model.bodies[1].edges = []
    result = f.add_thread(session, "M10x1.5", 0, 0, 20, 12)
    data = session.model.FeatureManager.created[-1]
    assert data.kind == 87 and data.BlindDepth == .012 and data.RightHanded
    assert data.Type == "Metric Die" and result["pitch_mm"] == 1.5


def test_thread_refuses_wrong_start_diameter(session):
    session.model.bodies[0].edges = [Edge(center=(0, 0, 0), diameter=.008)]
    with pytest.raises(CadError, match="circular"):
        f.add_thread(session, "M10x1.5", 0, 0, 0, 10)
    assert not session.model.FeatureManager.created


def test_chamfer_converts_distance_and_angle(session):
    f.add_chamfer(session, 1.5, angle_deg=30)
    args = session.model.FeatureManager.chamfers[-1]
    assert args[:3] == (4, 1, .0015) and math.isclose(args[3], math.pi / 6)


def test_linear_pattern_uses_normal_plane_and_keeps_seed_identity(session):
    result = f.add_linear_pattern(session, 4, 12, "-x", "Hole")
    definition = session.model.FeatureManager.created[-1]
    assert definition.D1Axis is session.planes["right"]
    assert definition.D1TotalInstances == 4 and definition.D1Spacing == .012
    assert definition.D1ReverseDirection and result["seed"] == "Hole"


def test_circular_pattern_creates_axis_through_origin_without_extra_planes(session):
    f.add_circular_pattern(session, 6, 0, 0, "Hole")
    definition = session.model.FeatureManager.created[-1]
    assert definition.TotalInstances == 6 and math.isclose(definition.Spacing, 2 * math.pi)
    assert definition.Axis.kind == "RefAxis"


@pytest.mark.parametrize("count", [1, 10001, True, 2.5])
def test_pattern_invalid_count_does_not_create_axis_or_feature(session, count):
    with pytest.raises(CadError):
        f.add_circular_pattern(session, count, 0, 0)
    assert len(session.model.features) == 2


def test_mirror_explicit_features_are_not_substituted(session):
    f.add_mirror(session, "right", features=["Hole"])
    args, selected = session.model.FeatureManager.mirrors[-1]
    assert args == (False, False, False, False, 0)
    assert selected[0] == (session.planes["right"], 2)
    assert selected[1][0].Name == "Hole" and selected[1][1] == 1


def test_closed_shell_has_no_removed_faces(session):
    result = f.add_shell(session, 1.5, open_face="none")
    assert session.model.shell_selection == []
    assert session.model.shell_arguments == (.0015, False)
    assert result["opened_faces"] == 0


def test_shell_requires_new_native_feature_after_void_insertion(session):
    session.model.shell_refused = True
    with pytest.raises(CadError, match="one new shell"):
        f.add_shell(session, 1.5, open_face="none")
    assert not session.model.FeatureManager.definitions


def test_shell_removal_face_uses_native_mark_one(session):
    face = object()
    session.resolve_face = lambda reference: face
    result = f.add_shell(session, 1.5, "+z")
    assert result["ok"]
    assert session.model.shell_selection == [(face, 1)]


class Face:
    def __init__(self, normal, center, planar=True):
        self.Normal, self.center, self.planar, self.edges = normal, center, planar, []

    def GetSurface(self):
        return SimpleNamespace(IsPlane=lambda: self.planar)

    def GetBox(self):
        return [*self.center, *self.center]

    def GetEdges(self):
        return self.edges


def test_full_round_selects_closest_opposite_side_pair_with_native_marks(session):
    central = Face([0, 1, 0], [0, .02, 0])
    sides = [Face([0, 0, 1], [0, 0, .002]), Face([0, 0, -1], [0, 0, -.002]),
             Face([1, 0, 0], [.05, 0, 0]), Face([-1, 0, 0], [-.05, 0, 0])]
    for side in sides:
        edge = Edge()
        edge.adjacent = [central, side]
        central.edges.append(edge)
    session.face = central
    result = f.add_full_round(session, "+y", 0, 20, 0)
    definition = session.model.FeatureManager.created[-1]
    assert result["width_mm"] == 4
    assert definition.initialized == 3 and definition.faces[4] == [central]
    assert [mark for _, mark in session.model.selection] == [2, 512, 4]
    assert set(id(v) for faces in definition.faces.values() for v in faces) == {id(central), id(sides[0]), id(sides[1])}


def test_full_round_refuses_nonplanar_center_before_native_feature(session):
    session.face = Face([0, 1, 0], [0, 0, 0], planar=False)
    with pytest.raises(CadError, match="planar"):
        f.add_full_round(session, "+y", 0, 0, 0)
    assert not session.model.FeatureManager.created


def test_full_round_ambiguous_equal_width_does_not_guess(session):
    central = Face([0, 1, 0], [0, .02, 0])
    for normal, point in [([0, 0, 1], [0, 0, .01]), ([0, 0, -1], [0, 0, -.01]),
                          ([1, 0, 0], [.01, 0, 0]), ([-1, 0, 0], [-.01, 0, 0])]:
        edge = Edge()
        edge.adjacent = [central, Face(normal, point)]
        central.edges.append(edge)
    session.face = central
    with pytest.raises(CadError, match="unique"):
        f.add_full_round(session, "+y", 0, 20, 0)
    assert not session.model.FeatureManager.created


def test_signed_linear_dimension_and_angular_units(session):
    result = f.set_dimension(session, "D1@Boss", -25)
    assert result["value"] == -25 and session.model.dimension.SystemValue == -.025
    session.model.dimension.kind = 1
    result = f.set_dimension(session, "D1@Boss", 90)
    assert result["unit"] == "deg" and math.isclose(session.model.dimension.SystemValue, math.pi / 2)


def test_dimension_silent_native_sign_normalization_rolls_back(session):
    session.model.dimension.normalize_positive = True
    result = f.set_dimension(session, "D1@Boss", -25)
    assert not result["ok"] and result["rollback"]["verified"]
    assert session.model.dimension.SystemValue == .01


def test_failed_dimension_rebuild_restores_original_value(session):
    session.model.rebuild_results = [False, True]
    result = f.set_dimension(session, "D1@Boss", 20)
    assert not result["ok"] and result["rollback"]["verified"] and session.model.dimension.SystemValue == .01


def test_reference_dimension_is_not_written(session):
    session.model.dimension.DrivenState = 1
    with pytest.raises(CadError):
        f.set_dimension(session, "D1@Boss", 20)
    assert session.model.dimension.SystemValue == .01


def test_dimension_listing_preserves_native_precision_and_filter(session):
    session.model.dimension._value = .012345678901
    result = f.list_dimensions(session, contains="boss")
    assert len(result["dimensions"]) == 1
    assert result["dimensions"][0]["value"] == .012345678901 * 1000
    assert f.list_dimensions(session, contains="missing")["count"] == 0


def test_history_failures_and_warnings_are_separate(session):
    session.model.features[1].error = 5
    session.model.features[1].warning = True
    data = f.list_features(session)
    assert data["features"][1]["warning"]
    assert f._feature_errors(session.model) == []


def test_delete_dry_run_leaves_native_equations_and_history_unchanged(session):
    session.model.equations.values.append('"D1@Boss" = 20mm')
    result = f.delete_feature(session, "Boss", dry_run=True)
    assert result["referencing_equations"] and len(session.model.features) == 2
    assert session.model.equations.GetCount() == 2


def test_delete_requires_explicit_equation_permission(session):
    session.model.equations.values.append('"D1@Boss" = 20mm')
    with pytest.raises(CadError, match="equations"):
        f.delete_feature(session, "Boss")
    assert len(session.model.features) == 2


def test_refused_feature_delete_restores_removed_equations(session):
    original = ['"Width" = 10mm', '"D1@Boss" = 20mm']
    session.model.equations.values = original[:]
    session.model.delete_refused = True
    result = f.delete_feature(session, "Boss", with_equations=True)
    assert not result["ok"] and result["rollback"]["verified"]
    assert session.model.equations.values == original


def test_suppression_readback_and_idempotent_noop(session):
    first = f.suppress_feature(session, "Boss")
    second = f.suppress_feature(session, "Boss")
    assert first["ok"] and first["suppressed"] and not second["changed"]


def test_suppression_failed_rebuild_restores_previous_state(session):
    session.model.rebuild_results = [False, True]
    result = f.suppress_feature(session, "Boss")
    assert not result["ok"] and result["rollback"]["verified"] and not session.model.features[0].suppressed


def test_reorder_verifies_native_history_order(session):
    result = f.reorder_feature(session, "Hole", "Boss")
    assert result["ok"] and [x.Name for x in session.model.features] == ["Hole", "Boss"]


def test_equation_batch_updates_and_appends_without_duplicate_lhs(session):
    result = f.set_equations(session, ['"Width" = 20mm', '"Height" = 30mm'])
    assert result["ok"] and session.model.equations.values == ['"Width" = 20mm', '"Height" = 30mm']


def test_invalid_late_equation_does_not_apply_earlier_changes(session):
    before = session.model.equations.values[:]
    with pytest.raises(CadError):
        f.set_equations(session, ['"Width" = 20mm', 'bad syntax'])
    assert session.model.equations.values == before


def test_native_rejected_equation_restores_whole_batch(session):
    before = session.model.equations.values[:]
    session.model.equations.reject_name = "Reject"
    result = f.set_equations(session, ['"Width" = 20mm', '"Reject" = 30mm'])
    assert not result["ok"] and result["rollback"]["verified"]
    assert session.model.equations.values == before


def test_delete_equation_by_exact_name_and_index(session):
    session.model.equations.values.append('"Height" = 30mm')
    assert f.delete_equation(session, "Width")["ok"]
    assert session.model.equations.values == ['"Height" = 30mm']
    assert f.delete_equation(session, "0")["ok"]
    assert not session.model.equations.values


def test_rgb_and_transparency_preserve_other_native_channels(session):
    before = session.model.MaterialPropertyValues[:]
    result = f.set_appearance(session, [255, 128, 0], .25)
    assert result["ok"] and result["rgb"] == [255, 128, 0] and result["transparency"] == .25
    assert session.model.MaterialPropertyValues[3:7] == before[3:7]


def test_custom_density_material_contains_only_user_specified_physics(session, monkeypatch, tmp_path):
    from solidworks_mcp_pro import storage
    monkeypatch.setattr(storage, "default_workspace", lambda: tmp_path)
    result = f.set_material(session, "User resin", density_kg_m3=1210)
    assert result["ok"] and result["density_only"]
    tree = ET.parse(result["database"])
    assert [e.tag for e in tree.iter() if e.tag == "DENS"] == ["DENS"]
    assert not any(e.tag in {"EX", "SIGYLD", "SIGXT"} for e in tree.iter())
    assert session.model.density == 1210
    assert tree.getroot().find("curves") is not None
    assert len(tree.getroot().findall("curves/point")) >= 2
    assert Path(result["database"]).read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    assert session.model.material_configurations == ["", ""]
    assert session.model.material_write_configurations == ["Default"]
    assert isinstance(result["previous_material"], list)
    assert json.loads(json.dumps(result))["previous_material"] == ["", ""]
    assert session.material_folders == "Original material libraries"
    assert session.material_refreshes == 2
    assert session.material_folder_writes[-1] == "Original material libraries"


def test_material_directory_is_registered_during_assignment_and_removed_after_failure(session, tmp_path):
    path = tmp_path / "materials.sldmat"
    path.write_text('<materials><material name="Test material"/></materials>')
    session.databases = [str(path)]
    original = session.material_folders

    def reject_assignment(config, database, name):
        assert str(tmp_path.resolve()) in session.material_folders.split(";")
        if name:
            raise RuntimeError("native material assignment failed")
        session.model.current_material = name, database

    session.model.SetMaterialPropertyName2 = reject_assignment
    result = f.set_material(session, "Test material")
    assert result["ok"] is False
    assert session.material_folders == original
    assert session.material_folder_writes[-1] == original


def test_throwing_registration_setter_cannot_leak_material_folder_changes(session, tmp_path):
    original = session.material_folders
    setter = session.set_material_folders

    def throw_after_setting(preference, value):
        setter(preference, value)
        if value != original:
            raise RuntimeError("registration setter threw after mutation")

    session.app.SetUserPreferenceStringValue = throw_after_setting
    with pytest.raises(CadError, match="MATERIAL_FOLDER_REGISTRATION_FAILED"):
        with f._material_database_folder(session, tmp_path / "material.sldmat"):
            pytest.fail("The handler must not run after registration fails")
    assert session.material_folders == original


def test_material_folder_restoration_failure_can_never_return_success(session, tmp_path):
    original = session.material_folders
    setter = session.set_material_folders

    def refuse_restoration(preference, value):
        return False if value == original else setter(preference, value)

    session.app.SetUserPreferenceStringValue = refuse_restoration
    with pytest.raises(CadError) as caught:
        with f._material_database_folder(session, tmp_path / "material.sldmat"):
            pass
    assert caught.value.code == "MATERIAL_FOLDER_RESTORATION_FAILED"
    assert caught.value.details["outcome_may_be_indeterminate"] is True


def test_material_with_multiple_configurations_uses_actual_active_configuration(session, tmp_path):
    path = tmp_path / "materials.sldmat"
    path.write_text('<materials><material name="AISI 1020"/></materials>')
    session.databases = [str(path)]
    session.model.configuration_count = 2
    result = f.set_material(session, "AISI 1020")
    assert result["ok"]
    assert session.model.material_configurations == ["Default", "Default"]


def test_installed_material_name_is_matched_exactly_not_guessed(session, tmp_path):
    path = tmp_path / "materials.sldmat"
    path.write_text('<materials><classification><material name="AISI 1020"><physicalproperties><DENS value="7850"/></physicalproperties></material></classification></materials>')
    session.databases = [str(path)]
    assert f.set_material(session, "aisi 1020")["material"] == "AISI 1020"
    with pytest.raises(CadError, match="Expected one"):
        f.set_material(session, "1020")


def test_material_failed_readback_restores_previous_assignment(session, tmp_path):
    path = tmp_path / "materials.sldmat"
    path.write_text('<materials><material name="New material"/></materials>')
    session.databases = [str(path)]
    session.model.current_material = ("Old material", "")
    writes = []

    def setter(config, database, name):
        writes.append(name)
        session.model.current_material = ("Wrong material", database) if name == "New material" else (name, database)

    session.model.SetMaterialPropertyName2 = setter
    result = f.set_material(session, "New material")
    assert not result["ok"] and result["rollback"]["verified"]
    assert writes == ["New material", "Old material"]
    assert session.model.current_material == ("Old material", "")
    assert result["previous_material"] == ["Old material", ""]
    assert json.loads(json.dumps(result))["previous_material"] == ["Old material", ""]


def test_read_sketch_transforms_local_points_to_model_millimetres(session):
    class MathPoint:
        def __init__(self, value):
            self.ArrayData = value

        def MultiplyTransform(self, transform):
            return MathPoint([a + b for a, b in zip(self.ArrayData, transform)])

    session.app.GetMathUtility = lambda: SimpleNamespace(CreatePoint=MathPoint)
    start = SimpleNamespace(X=.001, Y=.002, Z=0)
    end = SimpleNamespace(X=.011, Y=.002, Z=0)
    line = SimpleNamespace(GetType=lambda: 0, ConstructionGeometry=True, GetLength=lambda: .01,
                           GetStartPoint2=lambda: start, GetEndPoint2=lambda: end)
    sketch = SimpleNamespace(ModelToSketchTransform=SimpleNamespace(Inverse=lambda: [.01, .02, .03]),
                             GetSketchSegments=lambda: [line], GetConstrainedStatus=lambda: 3)
    session.model.features.append(Feature("UserSketch", session.model, "ProfileFeature", specific=sketch))
    result = f.read_sketch(session, "UserSketch")
    assert result["fully_defined"] and result["segments"][0]["construction"]
    assert result["segments"][0]["start_mm"] == [11, 22, 30]
    assert result["segments"][0]["end_mm"] == pytest.approx([21, 22, 30])


def test_read_sketch_does_not_adopt_non_sketch_feature(session):
    with pytest.raises(CadError, match="sketch"):
        f.read_sketch(session, "Boss")
