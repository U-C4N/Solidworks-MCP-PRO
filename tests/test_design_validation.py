"""Read-only design validation against native-shaped doubles, never live COM."""
from copy import deepcopy
from itertools import product
import json
import math
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from solidworks_mcp_pro import design_validation as design


IDENTITY = [1., 0., 0., 0., 1., 0., 0., 0., 1., 0., 0., 0., 1., 0., 0., 0.]


def check(kind="dimension", **kwargs):
    entry = {"id": "check-1", "kind": kind, "unit": design.UNITS.get(kind, "mm"),
             "abs_tolerance": 0., "expected": 10.}
    if kind == "dimension":
        entry["name"] = "D1@BlockExtrude"
    elif kind == "bbox_size":
        entry["axis"] = "z"
    elif kind == "distance":
        entry.update(component_a="Base-1", component_b="Lever-1")
    entry.update(kwargs)
    return entry


class Dimension:
    def __init__(self, name="D1@BlockExtrude", value=.01, kind=0):
        self.name, self.value, self.kind = name, value, kind
        self.on_read = None

    def GetNameForSelection(self):
        return self.name

    def GetType(self):
        return self.kind

    @property
    def SystemValue(self):
        if self.on_read:
            self.on_read()
        return self.value


class Body:
    def __init__(self, low=(0., 0., 0.), high=(.1, .07, .01)):
        self.points = list(product(*zip(low, high)))
        self.extremes = []
        self.reject = False

    def GetExtremePoint(self, *direction):
        self.extremes.append(direction)
        point = max(self.points, key=lambda p: sum(a * b for a, b in zip(p, direction)))
        return (not self.reject, *point)


class Sphere:
    """A rotated sphere must retain its diameter; rotating its old box would not."""
    def GetExtremePoint(self, *direction):
        length = math.hypot(*direction)
        return (True, *[.005 * value / length for value in direction])


class Mass:
    def __init__(self):
        self.Mass, self.Volume, self.Density = .5495, .00007, 7850.
        self.system, self.reject = False, False

    @property
    def UseSystemUnits(self):
        return self.system

    @UseSystemUnits.setter
    def UseSystemUnits(self, value):
        if not self.reject:
            self.system = value


class Measure:
    def __init__(self):
        self.Distance, self.IsIntersect = .002, False
        self.entities = None
        self.reject = self.raise_error = False

    def Calculate(self, entities):
        self.entities = entities
        if self.raise_error:
            raise RuntimeError("Explicit entity measurement unsupported by this native interface")
        return not self.reject


class Extension:
    def __init__(self):
        self.mass, self.measure = Mass(), Measure()
        self.mass_calls = self.measure_calls = 0

    def CreateMassProperty(self):
        self.mass_calls += 1
        return self.mass

    def CreateMeasure(self):
        self.measure_calls += 1
        return self.measure


class Component:
    def __init__(self, name, *, kind=1, state=2, body=None, transform=None):
        self.Name2, self.state = name, state
        self.child = SimpleNamespace(GetType=lambda: kind)
        self.bodies = [body or Body()]
        self.Transform2 = SimpleNamespace(ArrayData=transform or list(IDENTITY))

    def GetSuppression2(self):
        return self.state

    def GetModelDoc2(self):
        return self.child

    def GetBodies2(self, kind):
        assert kind == 0
        return self.bodies


class Model:
    def __init__(self, path, *, kind=1):
        self.path, self.kind = str(path), kind
        self.config, self.stamp, self.dirty = "Default", 2, False
        self.editing_self, self.sketch = True, None
        self.Extension = Extension()
        self.dimensions = {"D1@BlockExtrude": Dimension()}
        self.bodies = [Body()]
        self.components = [Component("Base-1"), Component("Lever-1")]
        self.top_components = self.components

    def GetPathName(self):
        return self.path

    def GetType(self):
        return self.kind

    def GetActiveConfiguration(self):
        return SimpleNamespace(Name=self.config)

    def GetUpdateStamp(self):
        return self.stamp

    def GetSaveFlag(self):
        return self.dirty

    def IsEditingSelf(self):
        return self.editing_self

    def GetActiveSketch2(self):
        return self.sketch

    def Parameter(self, name):
        return self.dimensions.get(name)

    def GetBodies2(self, kind, visible_only):
        assert kind == 0 and visible_only is False
        return self.bodies

    def GetComponents(self, top):
        return self.top_components if top else self.components

    def __getattr__(self, name):
        if name in {"EditRebuild", "ForceRebuild3", "ClearSelection2", "Save3", "SaveAs3", "ActivateDoc3"}:
            raise AssertionError(f"Validation attempted forbidden mutation {name}")
        raise AttributeError(name)


@pytest.fixture
def part(tmp_path):
    path = tmp_path / "part.sldprt"
    path.write_bytes(b"native part placeholder")
    model = Model(path)
    return SimpleNamespace(ActiveDoc=model), model, path


@pytest.fixture
def assembly(tmp_path):
    path = tmp_path / "assembly.sldasm"
    path.write_bytes(b"native assembly placeholder")
    model = Model(path, kind=2)
    return SimpleNamespace(ActiveDoc=model), model, path


def run(fixture, assertions):
    app, _, path = fixture
    result = design.validate_design(app, document_path=path, assertions=assertions)
    json.dumps(result, allow_nan=False)
    return result


def test_complete_part_design_measures_dimensions_mass_volume_and_tight_extents_once(part):
    entries = [check(), check("mass", id="mass", expected=.5495),
               check("volume", id="volume", expected=70000, abs_tolerance=1e-8),
               *[check("bbox_size", id=axis, axis=axis, expected=size, abs_tolerance=1e-9)
                 for axis, size in zip("xyz", (100, 70, 10))]]
    original = deepcopy(entries)
    result = run(part, entries)
    assert result["ok"] and result["passed"] and result["status"] == "pass"
    assert result["counts"] == {"total": 6, "passed": 6, "failed": 0, "unknown": 0}
    assert result["measurements"]["bbox_size_mm"] == [100, 70, 10]
    assert result["measurements"]["dimensions"]["D1@BlockExtrude"] == {"value": 10, "unit": "mm"}
    assert part[1].Extension.mass_calls == 1
    assert len(part[1].bodies[0].extremes) == 6
    assert result["state_unchanged"] and not result["mutation_attempted"]
    assert not result["saved"] and not result["rebuilt"]
    assert entries == original


def test_shared_measurement_reader_has_native_workflow_shape_and_never_rebuilds(part):
    data = design.read_design_measurements(part[1])
    assert data["mass_kg"] == .5495 and data["volume_mm3"] == pytest.approx(70000)
    assert data["density_kg_m3"] == 7850
    assert data["bounding_box_mm"]["size_mm"] == [100, 70, 10]
    assert part[1].stamp == 2 and not part[1].dirty


def test_failed_requirement_is_completed_scan_and_has_actual_expected_evidence(part):
    result = run(part, [check(expected=20)])
    assert result["ok"] and not result["passed"] and result["status"] == "fail"
    assert result["counts"]["failed"] == 1 and not result["errors"]
    assert result["assertions"][0]["actual"] == 10
    assert result["assertions"][0]["expected"] == 20
    assert result["assertions"][0]["evidence"]["system_unit"] == "m"


def test_native_angles_are_converted_to_degrees(part):
    dim = part[1].dimensions["D1@BlockExtrude"]
    dim.kind, dim.value = 1, math.pi / 2
    result = run(part, [check(unit="deg", expected=90)])
    assert result["passed"]
    assert result["assertions"][0]["evidence"]["system_value"] == math.pi / 2


@pytest.mark.parametrize("updates", [{"kind": 2}, {"kind": -1}, {"value": math.nan},
                                     {"name": "D1@OtherFeature"}, {"kind": 1}])
def test_unsupported_native_dimension_values_names_and_units_never_pass(part, updates):
    dim = part[1].dimensions["D1@BlockExtrude"]
    for name, value in updates.items():
        setattr(dim, name, value)
    result = run(part, [check()])
    assert not result["ok"] and not result["passed"]
    assert result["assertions"][0]["status"] == "unknown"


def test_missing_dimension_is_unknown_and_other_check_still_completes(part):
    result = run(part, [check(name="D7@NoSuchFeature"), check("bbox_size", id="height")])
    assert result["counts"] == {"total": 2, "passed": 1, "failed": 0, "unknown": 1}
    assert not result["ok"] and result["assertions"][0]["code"] == "DIMENSION_NOT_FOUND"


@pytest.mark.parametrize("expected,tolerance,relative,passed", [(10.5, .5, 0, True), (10.51, .5, 0, False),
                                                             (10.5, 0, .05, True), (11, 0, .05, False)])
def test_explicit_absolute_relative_tolerance_semantics(part, expected, tolerance, relative, passed):
    result = run(part, [check(expected=expected, abs_tolerance=tolerance, rel_tolerance=relative)])
    assert result["ok"] and result["passed"] is passed
    assert result["assertions"][0]["effective_tolerance"] == max(tolerance, relative * abs(expected))


def test_range_bounds_and_tolerance_are_inclusive(part):
    lower = check(id="lower", min=10.25, abs_tolerance=.25)
    upper = check(id="upper", max=9.75, abs_tolerance=.25)
    both = check(id="both", min=9.9, max=10.1)
    for entry in (lower, upper, both):
        entry.pop("expected")
    assert run(part, [lower, upper, both])["passed"]


def test_signed_linear_dimension_is_supported(part):
    part[1].dimensions["D1@BlockExtrude"].value = -.005
    assert run(part, [check(expected=-5)])["passed"]


@pytest.mark.parametrize("changes", [
    {"expected": True}, {"expected": math.nan}, {"expected": math.inf}, {"expected": 10**1000},
    {"abs_tolerance": -1}, {"abs_tolerance": None}, {"abs_tolerance": True},
    {"rel_tolerance": -1}, {"rel_tolerance": 1.01}, {"unit": "m"}, {"id": " "},
    {"name": ""}, {"kind": "python"}, {"script": "delete_all()"}, {"min": 0},
])
def test_invalid_assertions_are_rejected_before_any_native_access(changes):
    class Bomb:
        @property
        def ActiveDoc(self):
            raise AssertionError("Invalid assertion reached CAD")
    result = design.validate_design(Bomb(), document_path="unused.sldprt", assertions=[check(**changes)])
    assert not result["ok"] and result["errors"][0]["code"] == "INVALID_ARGUMENT"


def test_empty_duplicate_oversized_misordered_and_inapplicable_assertions_rejected():
    invalid = [[], [check()] * 2, [check(id=str(index)) for index in range(129)],
               [check("bbox_size", axis="w")], [check("mass", expected=-1)],
               [check("distance", component_b="Nested-1/Part-1")], [check("distance", component_b="Base-1")],
               [check("mass", name="D1@Sketch1")], [None]]
    reversed_range = check(min=10, max=9)
    reversed_range.pop("expected")
    invalid.extend([[reversed_range], [{"id": "no-range", "kind": "mass", "unit": "kg", "abs_tolerance": 0}]])
    for entries in invalid:
        with pytest.raises(design.DesignValidationError):
            design.validate_assertions(entries)


def test_assertion_preflight_normalizes_on_a_detached_copy():
    entries = [check(expected=10)]
    normalized = design.validate_assertions(entries)
    assert normalized[0]["rel_tolerance"] == 0
    assert type(normalized[0]["expected"]) is float
    normalized[0]["name"] = "Changed"
    assert entries[0]["name"] == "D1@BlockExtrude" and "rel_tolerance" not in entries[0]


@pytest.mark.parametrize("state", ["edit", "sketch", "drawing", "empty", "mismatch", "disconnected"])
def test_exact_active_saved_model_guard_precedes_measurements(part, tmp_path, state):
    app, model, path = part
    if state == "edit":
        model.editing_self = False
    elif state == "sketch":
        model.sketch = object()
    elif state == "drawing":
        model.kind = 3
    elif state == "empty":
        path.write_bytes(b"")
    elif state == "mismatch":
        other = tmp_path / "other.sldprt"
        other.write_bytes(b"other")
        model.path = str(other)
    else:
        app = None
    result = design.validate_design(app, document_path=path, assertions=[check("mass")])
    assert not result["ok"] and result["assertions"] == []
    assert model.Extension.mass_calls == 0


@pytest.mark.parametrize("change", ["stamp", "dirty", "configuration", "file", "active_document"])
def test_changed_document_state_invalidates_observed_success(part, tmp_path, change):
    app, model, path = part
    def modify():
        if change == "stamp":
            model.stamp += 1
        elif change == "dirty":
            model.dirty = True
        elif change == "configuration":
            model.config = "Other"
        elif change == "file":
            path.write_bytes(b"changed native file")
        else:
            other = tmp_path / "other.sldprt"
            other.write_bytes(b"other")
            app.ActiveDoc = Model(other)
    model.dimensions["D1@BlockExtrude"].on_read = modify
    result = run(part, [check()])
    assert not result["ok"] and not result["passed"] and not result["state_unchanged"]
    assert result["assertions"][0]["status"] == "unknown"
    assert result["assertions"][0]["observed_status"] == "pass"


@pytest.mark.parametrize("change", ["unit_refused", "nan_mass", "zero_volume", "overflow_volume", "missing_mp"])
def test_native_mass_failures_never_pass_and_cached_failure_is_not_retried(part, change):
    extension = part[1].Extension
    if change == "unit_refused":
        extension.mass.reject = True
    elif change == "nan_mass":
        extension.mass.Mass = math.nan
    elif change == "zero_volume":
        extension.mass.Volume = 0
    elif change == "overflow_volume":
        extension.mass.Volume = 1e308
    else:
        extension.mass = None
    result = run(part, [check("mass", id="mass"), check("volume", id="volume")])
    assert result["counts"]["unknown"] == 2 and not result["passed"]
    assert extension.mass_calls == 1


def test_assembly_mass_and_bbox_do_not_rebuild_or_activate(assembly):
    model = assembly[1]
    hidden = Component("Suppressed-1", state=0)
    hidden.child = None
    model.components.append(hidden)
    result = run(assembly, [check("mass", id="mass", expected=.5495),
                            check("bbox_size", id="size", expected=10)])
    assert result["passed"] and result["state_unchanged"]


@pytest.mark.parametrize("state", [1, 4, 7])
def test_assembly_incomplete_geometry_never_silently_passes_mass_or_bbox(assembly, state):
    assembly[1].components[0].state = state
    result = run(assembly, [check("mass", id="mass"), check("bbox_size", id="bbox")])
    assert result["counts"]["unknown"] == 2
    assert assembly[1].Extension.mass_calls == 0


def test_assembly_bbox_uses_transformed_body_extremes_not_rotated_old_boxes(assembly):
    c = math.sqrt(.5)
    transform = [c, c, 0., -c, c, 0., 0., 0., 1., .1, -.2, .3, 1., 0., 0., 0.]
    assembly[1].components = [Component("Ball-1", body=Sphere(), transform=transform)]
    result = run(assembly, [check("bbox_size", id=axis, axis=axis, abs_tolerance=1e-9) for axis in "xyz"])
    assert result["passed"]
    assert result["measurements"]["bounding_box_mm"]["min_mm"] == pytest.approx([95, -205, 295])
    assert result["measurements"]["bounding_box_mm"]["max_mm"] == pytest.approx([105, -195, 305])


def test_empty_or_failed_body_extreme_measurement_is_unknown(part):
    part[1].bodies[0].reject = True
    assert run(part, [check("bbox_size")])["assertions"][0]["code"] == "BBOX_UNAVAILABLE"
    part[1].bodies = []
    assert run(part, [check("bbox_size")])["assertions"][0]["code"] == "NO_SOLID_GEOMETRY"


def test_explicit_component_pair_distance_requires_no_selection_changes_and_caches_pairs(assembly):
    first = check("distance", id="first", expected=2)
    second = check("distance", id="second", expected=2, component_a="Lever-1", component_b="Base-1")
    result = run(assembly, [first, second])
    assert result["passed"] and assembly[1].Extension.measure_calls == 1
    assert [item.Name2 for item in assembly[1].Extension.measure.entities] == ["Base-1", "Lever-1"]
    assert result["assertions"][0]["evidence"]["scope"] == "top_level_loaded_part_pair"


def test_zero_unsigned_distance_is_not_mistaken_for_clearance(assembly):
    assembly[1].Extension.measure.IsIntersect = True
    assertion = check("distance", min=.01)
    assertion.pop("expected")
    result = run(assembly, [assertion])
    assert result["ok"] and not result["passed"] and result["assertions"][0]["actual"] == 0
    assert result["assertions"][0]["evidence"]["zero_means"] == "touching_or_overlapping"


@pytest.mark.parametrize("failure", ["reject", "raise", "missing", "subassembly", "unloaded", "lightweight", "no-solid"])
def test_unsupported_distance_cases_are_unknown_without_selection_fallback(assembly, failure):
    model = assembly[1]
    if failure == "reject":
        model.Extension.measure.reject = True
    elif failure == "raise":
        model.Extension.measure.raise_error = True
    elif failure == "missing":
        model.components[0].Name2 = "Different-1"
    elif failure == "subassembly":
        model.components[0].child = SimpleNamespace(GetType=lambda: 2)
    elif failure == "unloaded":
        model.components[0].child = None
    elif failure == "lightweight":
        model.components[0].state = 1
    else:
        model.components[0].bodies = []
    result = run(assembly, [check("distance")])
    assert not result["ok"] and result["assertions"][0]["status"] == "unknown"


def test_schema_contracts_exclude_arbitrary_operations_and_ambiguous_bounds():
    tool = design.design_tools()[0]
    Draft202012Validator.check_schema(tool.inputSchema)
    validator = Draft202012Validator(tool.inputSchema)
    valid = {"document_path": "C:\\Models\\part.sldprt", "assertions": [check()]}
    assert not list(validator.iter_errors(valid))
    invalid = deepcopy(valid)
    invalid["assertions"][0]["script"] = "arbitrary code"
    assert list(validator.iter_errors(invalid))
    invalid = deepcopy(valid)
    invalid["assertions"][0]["min"] = 1
    assert list(validator.iter_errors(invalid))
    assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
