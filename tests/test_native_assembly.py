"""Independent native contracts with observable fake COM state, no CAD launch."""

from pathlib import Path
from types import SimpleNamespace as NS
import math

import pytest

from solidworks_mcp_pro.native import assembly as api
from solidworks_mcp_pro.native import binding
from solidworks_mcp_pro.native.errors import CadError


IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]


class Face:
    def __init__(self, normal, low, high, edges=None):
        self.Normal = normal
        self.low, self.high = low, high
        self.edges = edges or []
        self.selected = []

    def GetSurface(self):
        return NS(IsPlane=lambda: True)

    def GetArea(self):
        return 0.0001

    def GetBox(self):
        return [*self.low, *self.high]

    def GetClosestPointOn(self, *point):
        return [max(a, min(b, p)) for a, b, p in zip(self.low, self.high, point)]

    def GetEdges(self):
        return self.edges

    def Select4(self, append, data):
        self.selected.append((append, data.Mark))
        return True

    def GetLoops(self):
        return [NS(IsOuter=lambda: True, GetEdges=lambda: self.edges)]


class Body:
    def __init__(self, low=(0.0, 0.0, 0.0), high=(0.01, 0.01, 0.01)):
        self.low, self.high = low, high
        self.faces = []
        for axis in range(3):
            for sign in (-1, 1):
                lo, hi = list(low), list(high)
                lo[axis] = hi[axis] = low[axis] if sign < 0 else high[axis]
                normal = [0.0, 0.0, 0.0]
                normal[axis] = float(sign)
                self.faces.append(Face(normal, lo, hi))

    def GetFaces(self):
        return self.faces

    def GetEdges(self):
        return []

    def GetExtremePoint(self, *direction):
        return (True, *[b if d > 0 else a for a, b, d in zip(self.low, self.high, direction)])


class Feature:
    def __init__(self, model, name, kind="MateCoincident"):
        self.model, self.Name, self.kind = model, name, kind
        self.suppressed = False

    def GetTypeName2(self):
        return self.kind

    def IsSuppressed(self):
        return self.suppressed

    def SetSuppression2(self, state, *args):
        self.suppressed = state == 0
        return True

    def Select2(self, append, mark):
        self.model.selected = self
        return True

    def GetNextFeature(self):
        keys = list(self.model.features)
        index = keys.index(self.Name) + 1
        return self.model.features[keys[index]] if index < len(keys) else None

    def GetFirstSubFeature(self):
        return None

    def GetErrorCode2(self):
        return (0, False)


class PartModel:
    def __init__(self, path="part.sldprt", kind=1):
        self.path = str(path)
        self.kind = kind
        self.stamp = 1
        self.dirty = False
        self.features = {}
        self.selected = None
        self.bodies = [Body()]
        self.components = []
        self.mass = NS(
            UseSystemUnits=False,
            Mass=0.001,
            Volume=1e-6,
            Density=1000.0,
            CenterOfMass=[0.005] * 3,
            SurfaceArea=0.0006,
        )
        self.Extension = NS(
            CreateMassProperty=lambda: self.mass,
            GetPersistReference3=lambda entity: list(id(entity).to_bytes(8, "little")),
            DeleteSelection2=self._delete,
        )
        self.SelectionManager = NS(CreateSelectData=lambda: NS(Mark=0))
        self.InterferenceDetectionManager = NS(
            TreatCoincidenceAsInterference=True,
            TreatSubAssembliesAsComponents=True,
            IncludeMultibodyPartInterferences=False,
            GetInterferences=lambda: [],
            GetInterferenceCount=lambda: 0,
            Done=lambda: None,
        )
        self.dim = NS(SystemValue=0.01, GetType=lambda: 0, DrivenState=2)
        self.clashes = []
        self.placement_calls = []

    def GetType(self):
        return self.kind

    def GetTitle(self):
        return Path(self.path).name

    def GetPathName(self):
        return self.path

    def GetUpdateStamp(self):
        return self.stamp

    def GetActiveConfiguration(self):
        return NS(Name="Default")

    def GetSaveFlag(self):
        return self.dirty

    def IsEditingSelf(self):
        return True

    def GetActiveSketch2(self):
        return None

    def GetBodies2(self, *args):
        return self.bodies

    def GetComponents(self, top):
        return self.components

    def GraphicsRedraw2(self):
        pass

    def ClearSelection2(self, *args):
        self.selected = None

    def FirstFeature(self):
        return next(iter(self.features.values()), None)

    def Parameter(self, name):
        return self.dim if name == "D1@Joint" else None

    def ForceRebuild3(self, top):
        if self.components:
            self.components[0].Transform2.ArrayData[9] = self.dim.SystemValue
        self.dirty = True
        return True

    def _delete(self, flags):
        if isinstance(self.selected, Feature):
            self.features.pop(self.selected.Name)
        else:
            self.components.remove(self.selected)
        return True

    def UnfixComponent(self):
        self.selected.fixed = False

    def FixComponent(self):
        self.selected.fixed = True

    def AddComponent5(self, path, *args):
        value = Component(self, f"Part-{len(self.components) + 1}", path)
        value.fixed = not self.components
        self.components.append(value)
        return value

    def AddMate5(self, *args):
        value = Feature(self, "Mate1")
        self.features[value.Name] = value
        return NS(GetMateEntityCount=lambda: 2), 1

    def ReplaceComponents2(self, path, *args):
        self.selected.path = str(path)
        return True

    def ClosestDistance(self, first, second):
        return 0.04, [0.0, 0.0, 0.0], [0.04, 0.0, 0.0]


class Component:
    def __init__(self, model, name, path="part.sldprt", x=0.0):
        self.model, self.Name2, self.path = model, name, str(path)
        self.child = PartModel(path)
        self.ReferencedConfiguration = "Default"
        self.Visible = 1
        self.fixed = False
        self.state = 2
        self.Transform2 = NS(ArrayData=IDENTITY.copy())
        self.Transform2.ArrayData[9] = x
        self.accept = True

    def GetPathName(self):
        return self.path

    def GetSuppression2(self):
        return self.state

    def GetModelDoc2(self):
        return self.child

    def IsFixed(self):
        return self.fixed

    def GetBodies2(self, kind):
        return self.child.bodies

    def GetCorrespondingEntity(self, value):
        return value

    def Select4(self, append, data, popup=False):
        self.model.selected = self
        return True

    def SetTransformAndSolve2(self, transform):
        if self.accept:
            self.Transform2 = transform
        return self.accept

    def GetBox(self, *args):
        return [self.Transform2.ArrayData[9], 0.0, 0.0, self.Transform2.ArrayData[9] + 0.01, 0.01, 0.01]


class Session:
    def __init__(self, model):
        self.model = model
        self.app = NS(
            GetMathUtility=lambda: NS(CreateTransform=lambda values: NS(ArrayData=list(values))),
            GetDocuments=lambda: [model],
            ActivateDoc3=lambda *args: None,
            CloseDoc=lambda *args: None,
        )

    def _require_model(self):
        return self.model

    def _require_part(self):
        assert self.model.kind == 1
        return self.model

    def _require_assembly(self):
        if self.model.kind != 2:
            raise CadError("ASSEMBLY_REQUIRED", "assembly required")
        return self.model

    def _ensure(self):
        return self.app

    def close_document(self, model):
        from solidworks_mcp_pro.native.session import NativeSession

        guard = NativeSession(self.app)
        guard._documents_rights = self._documents_rights
        result = guard.close_document(model)
        self._documents_rights = guard._documents_rights
        return result

    def find_feature(self, name):
        if name not in self.model.features:
            raise CadError("FEATURE_NOT_FOUND", "not found")
        return self.model.features[name]


@pytest.fixture(autouse=True)
def fake_native_arrays(monkeypatch):
    monkeypatch.setattr(binding, "array_double", lambda values: list(values))
    monkeypatch.setattr(binding, "array_dispatch", lambda values: list(values))


@pytest.fixture
def session(tmp_path):
    model = PartModel(tmp_path / "assembly.sldasm", 2)
    model.components = [Component(model, "A-1", x=0.01), Component(model, "B-1", x=0.06)]
    return Session(model)


def test_registry_has_all_fifteen_assembly_and_measurement_tools():
    assert len(api.TOOLS) == 15
    assert all(callable(value) for value in api.TOOLS.values())


def test_native_case_arguments_match_the_public_augmented_contracts():
    import runpy
    from jsonschema import Draft202012Validator
    from solidworks_mcp_pro.native.catalogue import contracts
    from solidworks_mcp_pro.validation import augment_schema

    cases = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/native_cases_assembly.py"))[
        "CASES"
    ]
    assert len(cases) == len({case["id"] for case in cases}) == 20
    for case in cases:
        for step in [*case["setup"], case]:
            Draft202012Validator(augment_schema(step["tool"], contracts()[step["tool"]])).validate(
                step["args"]
            )


def test_list_components_reports_exact_placements_and_states(session):
    result = api.list_components(session)
    assert result["count"] == 2 and result["components"][0]["position_mm"] == [10.0, 0.0, 0.0]
    session.model.components[0].state = 1
    assert api.list_components(session)["components"][0]["suppression"] == 1


def test_transform_uses_mm_and_xyz_rotations_with_readback(session):
    result = api.set_component_transform(session, "A-1", 20, 30, 40, rz_deg=90)
    values = result["component"]["transform"]
    assert values[9:12] == [0.02, 0.03, 0.04]
    assert values[:3] == pytest.approx([0, 1, 0], abs=1e-12)
    assert result["verified"] and result["mates_solved"]


def test_fixed_component_is_refused_before_placement(session):
    session.model.components[0].fixed = True
    with pytest.raises(CadError, match="COMPONENT_FIXED"):
        api.set_component_transform(session, "A-1", 10, 0, 0)


def test_failed_transform_restores_original_exact_matrix(session):
    target = session.model.components[0]
    before = target.Transform2.ArrayData.copy()
    target.accept = False
    with pytest.raises(CadError) as error:
        api.set_component_transform(session, "A-1", 20, 0, 0)
    assert error.value.details["restoration_verified"]
    assert target.Transform2.ArrayData == before


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, 10**400])
def test_invalid_coordinates_never_write_transform(session, value):
    before = session.model.components[0].Transform2
    with pytest.raises(CadError):
        api.set_component_transform(session, "A-1", value, 0, 0)
    assert session.model.components[0].Transform2 is before


def test_mass_properties_and_bounds_use_system_units_without_rebuild(monkeypatch):
    model = PartModel()
    session = Session(model)
    model.ForceRebuild3 = lambda *args: pytest.fail("read-only query rebuilt")
    result = api.get_mass_properties(session)["mass_properties"]
    assert result["volume_mm3"] == pytest.approx(1000)
    assert result["mass_kg"] == 0.001 and result["density_kg_m3"] == 1000
    assert result["center_of_mass_mm"] == [5.0, 5.0, 5.0]
    bounds = api.get_bounding_box(session)["bounding_box"]
    assert bounds["size_mm"] == [10.0, 10.0, 10.0] and bounds["max_z_mm"] == 10


def test_empty_native_volume_uses_the_shared_cad_error_contract():
    model = PartModel()
    model.mass.Volume = 0
    with pytest.raises(CadError) as error:
        api.get_mass_properties(Session(model))
    assert error.value.code == "NO_SOLID_GEOMETRY"


def test_assembly_bounds_include_component_translation(session):
    result = api.get_assembly_bounding_box(session)
    assert result["bounding_box"]["min_mm"] == [10.0, 0.0, 0.0]
    assert result["bounding_box"]["max_mm"] == pytest.approx([70.0, 10.0, 10.0])


def test_insert_places_source_origin_and_does_not_close_existing_part(session, tmp_path):
    path = tmp_path / "source.sldprt"
    path.write_bytes(b"native")
    source = PartModel(path)
    session.app.GetDocuments = lambda: [session.model, source]
    session.app.CloseDoc = lambda *args: pytest.fail("must not close pre-existing source")
    result = api.insert_component(session, str(path), 25, 35, 45, fixed=False)
    assert result["inserted"] and result["component"]["position_mm"] == [25.0, 35.0, 45.0]
    assert result["component"]["fixed"] is False


def _temporary_source_fixture(session, tmp_path, *, native_error=0, keep_visible=False):
    path = tmp_path / "temporary.sldprt"
    path.write_bytes(b"native")
    source = PartModel(path)
    foreign = PartModel(tmp_path / "foreign.sldprt")
    source.Visible, foreign.Visible, session.model.Visible = True, False, False
    documents, closed = [session.model, foreign], []
    session.app.ActiveDoc = session.model
    session.app.Visible = False
    session._model = session.model
    session._documents_rights = [{"model": session.model, "editable": True, "opened": True}]
    session.app.GetDocuments = lambda: list(documents)

    def open_doc(*args):
        documents.append(source)
        session.app.ActiveDoc = source
        return source, native_error, 0

    def activate(title, *args):
        assert title == session.model.GetTitle()
        session.app.ActiveDoc = session.model
        return session.model, 0

    def stale():
        pytest.fail("The closed source handle was dereferenced")

    def close(title):
        assert title == path.name
        assert foreign.Visible and session.model.Visible, (
            "The close helper must protect every hidden document"
        )
        closed.append(title)
        # Model the native side effect that also retires other hidden models.
        documents[:] = [doc for doc in documents if doc.Visible and (doc is not source or keep_visible)]
        session.app.Visible = True
        if not keep_visible:
            source.GetTitle = stale
            source.GetPathName = stale

    session.app.OpenDoc6 = open_doc
    session.app.ActivateDoc3 = activate
    session.app.CloseDoc = close
    return path, source, closed, foreign


def test_temporary_source_cleanup_uses_fresh_handles_and_keeps_assembly_ownership(session, tmp_path):
    path, source, closed, foreign = _temporary_source_fixture(session, tmp_path)
    rights = list(session._documents_rights)
    result = api.insert_component(session, str(path), 25, 0, 0, fixed=False)
    assert result["source_window"]["opened_by_operation"] and result["source_window"]["closed_by_operation"]
    assert result["source_window"]["cleanup"]["other_documents_preserved"] == 2
    assert result["source_window"]["cleanup"]["visibility_restored"]
    assert closed == [path.name] and session.app.ActiveDoc is session.model
    assert session._model is session.model and session._documents_rights == rights
    assert session.app.GetDocuments() == [session.model, foreign]
    assert not foreign.Visible and not session.model.Visible and not session.app.Visible
    assert not any(record["model"] is foreign for record in session._documents_rights)


def test_temporary_source_native_load_error_still_closes_only_its_window(session, tmp_path):
    path, source, closed, foreign = _temporary_source_fixture(session, tmp_path, native_error=2)
    before = list(session.model.components)
    with pytest.raises(CadError, match="COMPONENT_LOAD_FAILED"):
        api.insert_component(session, str(path), 25, 0, 0)
    assert closed == [path.name] and session.app.ActiveDoc is session.model
    assert session.model.components == before
    assert session.app.GetDocuments() == [session.model, foreign]
    assert not foreign.Visible and not session.app.Visible


def test_temporary_source_visible_after_close_is_not_reported_clean(session, tmp_path):
    path, source, closed, foreign = _temporary_source_fixture(session, tmp_path, keep_visible=True)
    with pytest.raises(CadError) as error:
        api.insert_component(session, str(path), 25, 0, 0)
    assert error.value.code == "COMPONENT_SOURCE_CLEANUP_FAILED"
    assert error.value.details["outcome_may_be_indeterminate"]
    assert not error.value.details["source_window"]["closed_by_operation"]
    assert session.app.ActiveDoc is session.model
    assert foreign in session.app.GetDocuments() and not foreign.Visible
    assert not session.app.Visible


def test_temporary_source_close_cannot_claim_success_when_another_document_disappears(session, tmp_path):
    path, source, closed, foreign = _temporary_source_fixture(session, tmp_path)
    original_close = session.app.CloseDoc

    def faulty_native_close(title):
        original_close(title)
        session.app.GetDocuments = lambda: [session.model]

    session.app.CloseDoc = faulty_native_close
    with pytest.raises(CadError) as error:
        api.insert_component(session, str(path), 25, 0, 0)
    assert error.value.code == "COMPONENT_SOURCE_CLEANUP_FAILED"
    assert "FOREIGN_DOCUMENT_CLOSED" in str(error.value)
    assert error.value.details["outcome_may_be_indeterminate"]
    assert session.app.ActiveDoc is session.model and not session.app.Visible


def test_wrong_existing_source_returned_by_open_is_never_closed(session, tmp_path):
    path = tmp_path / "requested.sldprt"
    path.write_bytes(b"native")
    foreign = PartModel(tmp_path / "foreign.sldprt")
    session.app.GetDocuments = lambda: [session.model, foreign]
    session.app.OpenDoc6 = lambda *args: (foreign, 0, 0)
    session.app.CloseDoc = lambda *args: pytest.fail("Foreign document must remain open")
    session.app.ActiveDoc = session.model
    session.app.ActivateDoc3 = lambda *args: (session.model, 0)
    with pytest.raises(CadError, match="COMPONENT_LOAD_FAILED"):
        api.insert_component(session, str(path), 25, 0, 0)


def test_insert_rejects_missing_file_before_native_insertion(session, tmp_path):
    before = len(session.model.components)
    with pytest.raises(CadError):
        api.insert_component(session, str(tmp_path / "missing.sldprt"))
    assert len(session.model.components) == before


def test_delete_component_verifies_removal(session):
    result = api.delete_component(session, "A-1")
    assert result["verified"] and [c.Name2 for c in session.model.components] == ["B-1"]


def test_replace_checks_exact_path_and_original_placement(session, tmp_path):
    replacement = tmp_path / "replacement.sldprt"
    replacement.write_bytes(b"native")
    result = api.replace_component(session, "A-1", str(replacement))
    assert result["verified"] and result["replacements"][0]["name"] == "A-1"


def test_replace_noop_does_not_use_unrelated_existing_instance_as_evidence(session, tmp_path):
    replacement = tmp_path / "replacement.sldprt"
    replacement.write_bytes(b"native")
    session.model.components[1].path = str(replacement)
    session.model.ReplaceComponents2 = lambda *args: True
    with pytest.raises(CadError, match="COMPONENT_REPLACE_UNVERIFIED"):
        api.replace_component(session, "A-1", str(replacement))


def test_add_mate_verifies_new_feature_and_selects_both_faces(session, monkeypatch):
    first = Face([0, 0, 1], [0, 0, 0], [0.01, 0.01, 0])
    second = Face([0, 0, -1], [0, 0, 0.01], [0.01, 0.01, 0.01])
    monkeypatch.setattr(api, "resolve", lambda s, ref, **kw: first if kw["component"] == "A-1" else second)
    result = api.add_mate(session, "A-1", "+z", "B-1", "-z")
    assert result["mate"] == "Mate1" and result["verified"]
    assert first.selected == [(False, 1)] and second.selected == [(True, 1)]


def test_failed_mate_native_error_is_not_success(session, monkeypatch):
    face = Face([0, 0, 1], [0, 0, 0], [0.01, 0.01, 0])
    monkeypatch.setattr(api, "resolve", lambda *args, **kwargs: face)
    session.model.AddMate5 = lambda *args: (None, 7)
    with pytest.raises(CadError, match="MATE_CREATION_FAILED"):
        api.add_mate(session, "A-1", "+z", "B-1", "-z")


@pytest.mark.parametrize("native_error", [0, 2, 3, 4, 5, 6])
def test_native_mate_unknown_zero_and_documented_errors_are_never_success(session, monkeypatch, native_error):
    face = Face([1, 0, 0], [0, 0, 0], [0, 0.01, 0.01])
    monkeypatch.setattr(api, "resolve", lambda *args, **kwargs: face)
    session.model.AddMate5 = lambda *args: (NS(GetMateEntityCount=lambda: 2), native_error)
    with pytest.raises(CadError) as error:
        api.add_mate(session, "A-1", "+x", "B-1", "-x")
    assert error.value.code == "MATE_CREATION_FAILED"
    assert error.value.details["native_error"] == native_error


@pytest.mark.parametrize("actual,wanted,error", [(0.02, 20, None), (0.01, 20, "MATE_DIMENSION_UNVERIFIED")])
def test_distance_mate_returns_exact_native_driving_dimension(session, monkeypatch, actual, wanted, error):
    face = Face([1, 0, 0], [0, 0, 0], [0, 0.01, 0.01])
    monkeypatch.setattr(api, "resolve", lambda *args, **kwargs: face)
    session.model.dim.SystemValue = actual
    session.model.dim.GetNameForSelection = lambda: "D1@Joint"
    calls = []

    def create(*args):
        calls.append(args)
        session.model.features["Distance1"] = Feature(session.model, "Distance1", "MateDistanceDim")
        return NS(
            GetMateEntityCount=lambda: 2,
            DisplayDimension2=lambda index: NS(GetDimension2=lambda config: session.model.dim),
        ), 1

    session.model.AddMate5 = create
    if error is not None:
        with pytest.raises(CadError, match=error):
            api.add_mate(session, "A-1", "+x", "B-1", "-x", "distance", distance_mm=wanted)
    else:
        result = api.add_mate(session, "A-1", "+x", "B-1", "-x", "distance", distance_mm=wanted)
        assert result["dimension_name"] == "D1@Joint"
        assert result["mate"] == "Distance1"
    assert calls[0][1] == 2 and calls[0][3] == wanted / 1000


def test_suppress_and_delete_exact_mate(session):
    mate = Feature(session.model, "Mate1")
    session.model.features["Mate1"] = mate
    assert api.suppress_mate(session, "Mate1")["suppressed"]
    assert api.delete_mate(session, "Mate1")["verified"] and not session.model.features


def test_native_delete_report_without_removing_mate_is_not_success(session):
    session.model.features["Mate1"] = Feature(session.model, "Mate1")
    session.model.Extension.DeleteSelection2 = lambda flags: True
    with pytest.raises(CadError, match="MATE_DELETE_UNVERIFIED"):
        api.delete_mate(session, "Mate1")
    assert "Mate1" in session.model.features


def test_nonmate_feature_cannot_be_deleted_as_mate(session):
    session.model.features["Boss"] = Feature(session.model, "Boss", "Boss")
    with pytest.raises(CadError, match="MATE_REQUIRED"):
        api.delete_mate(session, "Boss")
    assert "Boss" in session.model.features


def test_interference_restores_manager_options_and_reports_material_overlap(session):
    manager = session.model.InterferenceDetectionManager
    manager.GetInterferences = lambda: [NS(Components=session.model.components, Volume=2e-9)]
    manager.GetInterferenceCount = lambda: 1
    result = api.check_interference(session)
    assert result["count"] == 1 and result["interferences"][0]["volume_mm3"] == 2
    assert result["cleanup"]["verified"]
    assert manager.TreatCoincidenceAsInterference and not manager.IncludeMultibodyPartInterferences


def test_interference_failure_still_restores_options(session):
    manager = session.model.InterferenceDetectionManager

    def fail():
        raise RuntimeError("native scan failure")

    manager.GetInterferences = fail
    with pytest.raises(RuntimeError):
        api.check_interference(session)
    assert manager.TreatCoincidenceAsInterference and manager.TreatSubAssembliesAsComponents


def test_interference_partial_loading_is_not_reported_clear(session):
    session.model.components[0].state = 1
    with pytest.raises(CadError, match="COMPONENT_NOT_RESOLVED"):
        api.check_interference(session)


def test_native_component_distance_is_mm_with_both_closest_points(session):
    result = api.measure_distance(session, "A-1", "B-1")
    assert result["distance_mm"] == 40 and result["point_b_mm"] == [40.0, 0.0, 0.0]
    assert result["temporary_geometry_inserted"] is False


@pytest.mark.parametrize(
    "arguments", [{}, {"component_b": "B-1", "point_mm": [0, 0, 0]}, {"component_b": "A-1"}]
)
def test_distance_requires_exactly_one_distinct_target(session, arguments):
    with pytest.raises(CadError):
        api.measure_distance(session, "A-1", **arguments)


def test_point_measurement_uses_exact_temporary_vertex_not_inserted_sketch(session, monkeypatch):
    seen = []

    def line(s, a, b):
        seen.append((a, b))
        return object(), NS(GetStartVertex=lambda: NS(GetPoint=lambda: a))

    monkeypatch.setattr(api, "_temporary_line", line)
    result = api.measure_distance(session, "A-1", point_mm=[10, 20, 30])
    assert seen[0][0] == [0.01, 0.02, 0.03] and result["distance_mm"] == 40


def test_axis_measurement_clips_beyond_entire_component_projection(session, monkeypatch):
    seen = []
    monkeypatch.setattr(api, "_temporary_line", lambda s, a, b: (seen.append((a, b)), object()))
    result = api.measure_distance(session, "A-1", axis_mm=[0, 0, 0, 1, 0, 0])
    assert seen[0][0][0] < 0.01 and seen[0][1][0] > 0.02
    assert result["target"]["axis_semantics"].startswith("infinite_line")


def test_nested_axis_points_keep_an_offset_line_parallel_to_their_difference(session, monkeypatch):
    seen = []
    monkeypatch.setattr(api, "_temporary_line", lambda s, a, b: (seen.append((a, b)), object()))
    result = api.measure_distance(session, "A-1", axis_mm=[[10, 20, 30], [20, 20, 30]])
    start, end = seen[0]
    assert start[0] < 0.01 and end[0] > 0.02
    assert start[1:] == end[1:] == [0.02, 0.03]
    assert result["target"]["axis_definition"] == "two_points_mm"


@pytest.mark.parametrize(
    "distances", [[{"component_a": "A-1", "component_b": "B-1", "min_mm": 30}], [["A-1", "B-1"]]]
)
def test_motion_checks_samples_and_restores_dimension_and_component_poses(session, distances):
    before = [c.Transform2.ArrayData.copy() for c in session.model.components]
    result = api.check_motion(session, "D1@Joint", [10, 20], distances=distances)
    assert result["passed"] and len(result["samples"]) == 2 and result["restoration"]["verified"]
    assert (
        session.model.dim.SystemValue == 0.01
        and [c.Transform2.ArrayData for c in session.model.components] == before
    )
    assert result["continuous_motion_verified"] is False


def test_motion_restore_failure_is_indeterminate(session):
    calls = []

    def rebuild(top):
        calls.append(1)
        return len(calls) == 1

    session.model.ForceRebuild3 = rebuild
    result = api.sample_dimension(session, "D1@Joint", [20], lambda value: {"passed": True})
    assert not result["ok"] and result["status"] == "indeterminate" and not result["restoration"]["verified"]


def test_printability_excludes_bed_faces_and_flags_unsupported_ceiling(session, monkeypatch):
    from solidworks_mcp_pro.native import mesh

    session = Session(PartModel())

    triangles = [((0, 0, 0), (1, 0, 0), (0, 1, 0)), ((0, 0, 1), (0, 1, 1), (1, 0, 1))]
    monkeypatch.setattr(mesh, "native_triangles", lambda s: triangles)
    result = api.check_printability(session)
    assert result["passed"] is False and result["overhang_area_mm2"] == 0.5


def test_requested_wall_check_is_unknown_when_planar_samples_are_incomplete(session, monkeypatch):
    from solidworks_mcp_pro.native import mesh

    session = Session(PartModel())

    monkeypatch.setattr(mesh, "native_triangles", lambda s: [((0, 0, 0), (1, 0, 0), (0, 1, 0))])
    monkeypatch.setattr(api, "face_records", lambda s: [])
    result = api.check_printability(session, min_wall_mm=1)
    assert result["status"] == "unknown" and result["passed"] is None


def test_rotation_matrix_remains_rigid():
    values = api._placement(0, 0, 0, 23, 41, -17)
    columns = [values[i : i + 3] for i in (0, 3, 6)]
    for i in range(3):
        for j in range(3):
            assert sum(a * b for a, b in zip(columns[i], columns[j])) == pytest.approx(1 if i == j else 0)
    assert math.hypot(*columns[0]) == pytest.approx(1)
