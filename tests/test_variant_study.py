"""Variant isolation and failure handling against native-shaped doubles, no COM."""
from copy import deepcopy
import csv
import json
import math
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from solidworks_mcp_pro import variant_study as study


DIMENSION = "D1@BlockExtrude"
SECOND_DIMENSION = "D2@Sketch1"


def parameter(values=(8, 4, 6), *, dimension=DIMENSION, unit="mm"):
    return {"dimension": dimension, "unit": unit, "values": list(values)}


class Dimension:
    def __init__(self, model, name, value, kind=0):
        self.model, self.name, self.value, self.kind = model, name, value, kind
        self.DrivenState = 2
        self.ignore, self.absolute_only, self.on_set = False, False, None

    def GetNameForSelection(self):
        return self.name

    def GetType(self):
        return self.kind

    @property
    def SystemValue(self):
        return self.value

    @SystemValue.setter
    def SystemValue(self, value):
        self.model.writes.append((self.name, value))
        if not self.ignore:
            self.value = abs(value) if self.absolute_only else value
            self.model.dirty = True
            self.model.stamp += 1
        if self.on_set:
            self.on_set()


class Body:
    def __init__(self, model):
        self.model = model

    def GetExtremePoint(self, *direction):
        return True, *(size if value > 0 else 0. for value, size in zip(direction, self.model.size()))


class Mass:
    def __init__(self, model):
        self.model, self.system = model, False
        self.reject_units = False

    @property
    def UseSystemUnits(self):
        return self.system

    @UseSystemUnits.setter
    def UseSystemUnits(self, value):
        if not self.reject_units:
            self.system = value

    @property
    def Volume(self):
        return math.prod(self.model.size())

    @property
    def Mass(self):
        return self.Volume * self.Density

    @property
    def Density(self):
        return self.model.density


class Model:
    def __init__(self, path, *, kind=1):
        self.path, self.kind = str(path), kind
        self.config, self.stamp, self.dirty = "Default", 1, False
        self.read_only, self.view_only, self.sketch = False, False, None
        self.writes, self.saves, self.rebuilds = [], [], []
        self.rebuild_ok, self.save_ignored, self.failed_feature = True, False, False
        self.density = 7850.
        self.dimensions = {DIMENSION: Dimension(self, DIMENSION, .004),
                           SECOND_DIMENSION: Dimension(self, SECOND_DIMENSION, .020)}
        self.mass = Mass(self)
        self.Extension = SimpleNamespace(CreateMassProperty=lambda: self.mass)

    def size(self):
        return abs(self.dimensions[DIMENSION].value), abs(self.dimensions[SECOND_DIMENSION].value), .01

    def GetPathName(self):
        return self.path

    def GetTitle(self):
        return Path(self.path).name if self.path else "UntitledUserPart"

    def GetType(self):
        return self.kind

    def GetSaveFlag(self):
        return self.dirty

    def GetUpdateStamp(self):
        return self.stamp

    def GetActiveConfiguration(self):
        return SimpleNamespace(Name=self.config)

    def GetActiveSketch2(self):
        return self.sketch

    def IsEditingSelf(self):
        return True

    def IsOpenedReadOnly(self):
        return self.read_only

    def IsOpenedViewOnly(self):
        return self.view_only

    def Parameter(self, name):
        return self.dimensions.get(name)

    def ForceRebuild3(self, top_only):
        assert top_only is False
        self.rebuilds.append(top_only)
        self.stamp += 1
        return self.rebuild_ok

    def FirstFeature(self):
        if not self.failed_feature:
            return None
        return SimpleNamespace(Name="BrokenCut", GetTypeName2=lambda: "Cut",
            GetErrorCode2=lambda: (1, False), GetNextFeature=lambda: None, GetFirstSubFeature=lambda: None)

    def SaveAs3(self, path, version, options):
        assert path == self.path and version == 0 and options == 1
        self.saves.append(path)
        if not self.save_ignored:
            Path(path).write_text(json.dumps({name: dimension.value for name, dimension in self.dimensions.items()}), encoding="utf-8")
            self.dirty = False
        return 0

    def GetBodies2(self, kind, visible_only):
        assert kind == 0 and visible_only is False
        return [Body(self)]


class App:
    def __init__(self, source):
        self.source = source
        self.ActiveDoc = source
        self.documents = [source]
        self.opened, self.closed, self.activations, self.candidates = [], [], [], []
        self.dependencies = None
        self.open_hook = None
        self.wrong_open_document, self.refuse_close, self.refuse_restore = False, False, False

    def GetDocuments(self):
        return tuple(self.documents)

    def GetDocumentDependencies2(self, path, traverse, search, read_only):
        assert path == self.source.path and traverse is True and search is False and read_only is False
        return self.dependencies

    def OpenDoc6(self, path, kind, options, configuration, errors, warnings):
        assert kind == 1 and options == 1 and configuration == "" and errors == warnings == 0
        self.opened.append(path)
        candidate = Model(path)
        self.documents.append(candidate)
        self.candidates.append(candidate)
        self.ActiveDoc = candidate
        if self.open_hook:
            self.open_hook(candidate)
        return (self.source if self.wrong_open_document else candidate, 0, 0)

    def CloseDoc(self, title):
        assert title != self.source.GetTitle(), "Never close the source"
        self.closed.append(title)
        if self.refuse_close:
            return
        self.documents = [model for model in self.documents if model.GetTitle() != title]
        if self.ActiveDoc is not None and self.ActiveDoc.GetTitle() == title:
            self.ActiveDoc = self.documents[0] if self.documents else None

    def ActivateDoc3(self, title, use_user_preferences, rebuild_option, errors):
        assert use_user_preferences is False and rebuild_option == 1 and errors == 0
        self.activations.append(title)
        if self.refuse_restore and title == self.source.GetTitle():
            return None, 1
        self.ActiveDoc = next((model for model in self.documents if model.GetTitle() == title), None)
        return self.ActiveDoc, 0


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "source.sldprt"
    source.write_bytes(b"saved native fixture bytes")
    app = App(Model(source))
    return source, app, tmp_path / "variants"


def run(fixture, *, parameters=None, **kwargs):
    source, app, target = fixture
    return study.run_variant_study(app, document_path=source, output_directory=target,
                                   parameters=parameters or [parameter()], **kwargs)


def assert_source_untouched(fixture):
    source, app, _ = fixture
    assert not app.source.writes and not app.source.saves and not app.source.rebuilds
    assert app.source in app.documents
    assert source.read_bytes() == b"saved native fixture bytes"


@pytest.mark.parametrize("parameters", [
    [], [parameter()] * 3, [parameter(unit="cm")], [parameter(unit=[])],
    [parameter(values=[])], [parameter(values=[1, 1.0])],
    [parameter(values=[True])], [parameter(values=[float("nan")])],
    [parameter(values=[float("inf")])], [parameter(values=[10**309])],
    [parameter(dimension="guess")], [parameter(dimension="D1@Bad\nName")],
    [parameter(), parameter(dimension=DIMENSION.lower())],
    [parameter(range(5)), parameter(range(4), dimension=SECOND_DIMENSION)],
])
def test_entire_cartesian_input_is_validated_before_any_native_work(parameters):
    with pytest.raises(study.VariantStudyError) as exc:
        study.normalize_study_parameters(parameters)
    assert exc.value.code == "INVALID_ARGUMENT"


def test_normalization_copies_input_and_keeps_signed_angular_values():
    parameters = [parameter([-30, 0, 30], unit="deg")]
    original = deepcopy(parameters)
    normalized = study.normalize_study_parameters(parameters)
    normalized[0]["values"][0] = 180
    assert parameters == original


def test_dry_run_writes_nothing_and_does_not_open_activate_rebuild_or_save(fixture):
    result = run(fixture)
    assert result["ok"] and result["status"] == "validated"
    assert result["counts"]["planned"] == 3 and result["counts"]["evaluated"] == 0
    assert result["source_unchanged"] and result["source_document_unchanged"]
    assert result["active_document_restored"]
    assert not fixture[2].exists()
    assert not fixture[1].opened and not fixture[1].activations
    assert_source_untouched(fixture)


def test_dry_run_reports_deferred_dimension_check_for_closed_source(fixture):
    _, app, _ = fixture
    app.documents, app.ActiveDoc = [], None
    result = run(fixture)
    assert result["status"] == "validated"
    assert result["source_dimensions_preflight"] == "deferred_until_owned_copy_is_opened"
    assert not app.opened


def test_variants_are_independent_ranked_saved_and_reported(fixture):
    source, app, _ = fixture
    result = run(fixture, dry_run=False)
    assert result["ok"] and result["status"] == "completed", result
    assert result["source_unchanged"] and result["source_document_unchanged"]
    assert result["counts"] == {"planned": 3, "evaluated": 3, "valid": 3, "invalid": 0, "unknown": 0}
    assert [item["id"] for item in result["ranked_variants"]] == ["variant_002", "variant_003", "variant_001"]
    assert result["best_variant"]["id"] == "variant_002"
    assert app.ActiveDoc is app.source and len(app.documents) == 1
    assert len(app.opened) == len(app.closed) == 3
    assert all(candidate.writes[0][1] in {.004, .006, .008} for candidate in app.candidates)
    assert all(candidate.dimensions[SECOND_DIMENSION].value == .020 for candidate in app.candidates)
    for variant in result["variants"]:
        assert variant["saved"] and Path(variant["path"]).is_file()
        assert Path(variant["path"]) != source
        assert variant["sha256"] == study._sha(Path(variant["path"]))
        assert variant["measurements"]["bounding_box_mm"]["size_mm"][1:] == [20., 10.]
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["source_sha256_before"] == manifest["source_sha256_after"]
    assert manifest["ranked_variants"] == result["ranked_variants"]
    with Path(result["csv_path"]).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 3 and rows[1][f"parameter[{DIMENSION}]_mm"] == "4.0"
    assert_source_untouched(fixture)


def test_two_parameter_study_enumerates_explicit_product(fixture):
    result = run(fixture, parameters=[parameter([4, 8]), parameter([20, 30], dimension=SECOND_DIMENSION)], dry_run=False)
    assert result["counts"]["valid"] == 4
    assert [row["parameters"] for row in result["variants"]] == [
        {DIMENSION: 4., SECOND_DIMENSION: 20.}, {DIMENSION: 4., SECOND_DIMENSION: 30.},
        {DIMENSION: 8., SECOND_DIMENSION: 20.}, {DIMENSION: 8., SECOND_DIMENSION: 30.},
    ]
    assert_source_untouched(fixture)


def test_design_conditions_filter_the_lightest_valid_ranking(fixture):
    assertions = [{"id": "mass-limit", "kind": "mass", "unit": "kg", "max": .008, "abs_tolerance": 1e-9}]
    result = run(fixture, assertions=assertions, dry_run=False)
    assert result["ok"] and result["counts"]["valid"] == 1 and result["counts"]["invalid"] == 2
    assert result["best_variant"]["id"] == "variant_002"
    assert all(row["saved"] for row in result["variants"])
    assert not result["variants"][0]["validation"]["passed"]
    assert_source_untouched(fixture)


def test_invalid_assertion_is_rejected_before_native_calls_or_files(fixture):
    result = run(fixture, assertions=[{"kind": "mass"}], dry_run=False)
    assert not result["ok"] and result["status"] == "rejected"
    assert not fixture[2].exists() and not fixture[1].opened
    assert_source_untouched(fixture)


@pytest.mark.parametrize("change,code", [
    (lambda app: setattr(app.source, "dirty", True), "SOURCE_UNSAVED"),
    (lambda app: setattr(app.source, "sketch", object()), "DOCUMENT_EDIT_MODE"),
    (lambda app: setattr(app.source.dimensions[DIMENSION], "kind", 2), "DIMENSION_TYPE_UNSUPPORTED"),
    (lambda app: setattr(app.source.dimensions[DIMENSION], "kind", -1), "DIMENSION_TYPE_UNSUPPORTED"),
    (lambda app: setattr(app.source.dimensions[DIMENSION], "DrivenState", 1), "DIMENSION_NOT_DRIVING"),
    (lambda app: setattr(app.source.dimensions[DIMENSION], "name", "D1@AnotherFeature"), "DIMENSION_NAME_MISMATCH"),
    (lambda app: setattr(app, "dependencies", ["Base", "C:/other.sldprt"]), "SOURCE_REFERENCES_UNSUPPORTED"),
])
def test_source_preflight_refuses_unsafe_or_unknown_inputs_without_opening_copies(fixture, change, code):
    change(fixture[1])
    result = run(fixture, dry_run=False)
    assert not result["ok"] and result["error"]["code"] == code
    assert not fixture[2].exists() and not fixture[1].opened
    assert_source_untouched(fixture)


def test_existing_output_directory_is_never_used_or_overwritten(fixture):
    fixture[2].mkdir()
    sentinel = fixture[2] / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = run(fixture, dry_run=False)
    assert result["error"]["code"] == "OUTPUT_EXISTS_OR_INVALID"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not fixture[1].opened


@pytest.mark.parametrize("failure,code", [
    (lambda model: setattr(model, "rebuild_ok", False), "VARIANT_REBUILD_FAILED"),
    (lambda model: setattr(model, "failed_feature", True), "VARIANT_FEATURE_ERRORS"),
    (lambda model: setattr(model.dimensions[DIMENSION], "ignore", True), "DIMENSION_READBACK_MISMATCH"),
    (lambda model: setattr(model, "save_ignored", True), "VARIANT_SAVE_FAILED"),
    (lambda model: setattr(model.mass, "reject_units", True), "MASS_UNIT_UNVERIFIED"),
    (lambda model: setattr(model, "density", 0.), "SOLID_MASS_UNAVAILABLE"),
])
def test_failed_candidates_are_not_ranked_but_other_candidates_continue(fixture, failure, code):
    fixture[1].open_hook = failure
    result = run(fixture, parameters=[parameter([8])], dry_run=False)
    assert result["ok"] and result["counts"]["valid"] == 0
    assert result["variants"][0]["error"]["code"] == code
    assert result["ranked_variants"] == [] and result["best_variant"] is None
    assert len(fixture[1].closed) == 1
    assert_source_untouched(fixture)


def test_all_dimensions_are_resolved_before_the_first_candidate_write(fixture):
    app = fixture[1]
    app.documents, app.ActiveDoc = [], None
    app.open_hook = lambda model: model.dimensions.pop(SECOND_DIMENSION)
    result = run(fixture, parameters=[parameter([8]), parameter([20], dimension=SECOND_DIMENSION)], dry_run=False)
    assert result["variants"][0]["error"]["code"] == "DIMENSION_NOT_FOUND"
    assert not app.candidates[0].writes
    assert app.ActiveDoc is None


def test_a_native_sign_change_is_not_accepted_as_the_requested_value(fixture):
    fixture[1].open_hook = lambda model: setattr(model.dimensions[DIMENSION], "absolute_only", True)
    result = run(fixture, parameters=[parameter([-8])], dry_run=False)
    assert result["variants"][0]["error"]["code"] == "DIMENSION_READBACK_MISMATCH"
    assert not result["ranked_variants"]


def test_second_parameter_cannot_silently_reset_the_first(fixture):
    def couple_dimensions(model):
        model.dimensions[SECOND_DIMENSION].on_set = lambda: setattr(model.dimensions[DIMENSION], "value", .004)
    fixture[1].open_hook = couple_dimensions
    result = run(fixture, parameters=[parameter([8]), parameter([30], dimension=SECOND_DIMENSION)], dry_run=False)
    assert result["variants"][0]["error"]["code"] == "DIMENSION_READBACK_MISMATCH"
    assert not result["ranked_variants"]
    assert_source_untouched(fixture)


def test_each_candidate_opens_original_saved_bytes_and_initial_dimensions(fixture):
    opened_bytes = []
    def inspect_copy(model):
        opened_bytes.append(Path(model.path).read_bytes())
        assert model.dimensions[DIMENSION].value == .004
    fixture[1].open_hook = inspect_copy
    result = run(fixture, dry_run=False)
    assert result["ok"] and len(opened_bytes) == 3
    assert set(opened_bytes) == {b"saved native fixture bytes"}


def test_angular_parameters_use_radians_and_require_matching_native_type(fixture):
    fixture[1].source.dimensions[DIMENSION].kind = 1
    fixture[1].open_hook = lambda model: setattr(model.dimensions[DIMENSION], "kind", 1)
    result = run(fixture, parameters=[parameter([30], unit="deg")], dry_run=False)
    assert result["counts"]["valid"] == 1
    assert fixture[1].candidates[0].writes[0][1] == pytest.approx(math.pi / 6)
    assert result["variants"][0]["dimension_readback"][DIMENSION]["actual"] == pytest.approx(30)


def test_opening_the_wrong_document_does_not_mutate_or_close_it(fixture):
    fixture[1].wrong_open_document = True
    result = run(fixture, parameters=[parameter([8])], dry_run=False)
    assert result["variants"][0]["error"]["code"] == "COPY_DOCUMENT_MISMATCH"
    assert_source_untouched(fixture)


def test_source_disk_change_blocks_ranking_and_is_reported_as_indeterminate(fixture):
    fixture[1].open_hook = lambda model: fixture[0].write_bytes(b"external source edit")
    result = run(fixture, dry_run=False)
    assert not result["ok"] and result["status"] == "indeterminate"
    assert not result["source_unchanged"] and not result["ranked_variants"]
    assert not fixture[1].source.saves and not fixture[1].source.writes


def test_source_memory_change_is_detected_even_if_disk_hash_matches(fixture):
    fixture[1].open_hook = lambda model: setattr(fixture[1].source, "stamp", 99)
    result = run(fixture, parameters=[parameter([8])], dry_run=False)
    assert not result["ok"] and result["status"] == "indeterminate"
    assert result["source_unchanged"] and not result["source_document_unchanged"]
    assert not result["ranked_variants"]


def test_copy_close_failure_retains_evidence_and_stops_following_variants(fixture):
    fixture[1].refuse_close = True
    result = run(fixture, dry_run=False)
    assert result["status"] == "indeterminate" and not result["ok"]
    assert len(result["variants"]) == 1 and len(fixture[1].opened) == 1
    assert result["cleanup"]["errors"][0]["code"] == "COPY_CLOSE_UNVERIFIED"
    assert Path(result["manifest_path"]).is_file()
    assert not result["ranked_variants"]
    assert_source_untouched(fixture)


def test_active_document_restoration_failure_is_not_hidden(fixture):
    app = fixture[1]
    original_close = app.CloseDoc
    def close_without_reactivating(title):
        original_close(title)
        app.ActiveDoc = None
    app.CloseDoc = close_without_reactivating
    app.refuse_restore = True
    result = run(fixture, parameters=[parameter([8])], dry_run=False)
    assert not result["ok"] and result["status"] == "indeterminate"
    assert not result["active_document_restored"]
    assert not result["ranked_variants"]
    assert_source_untouched(fixture)


def test_no_connection_is_reported_without_launching_cad(fixture):
    result = study.run_variant_study(None, document_path=fixture[0], output_directory=fixture[2], parameters=[parameter()])
    assert result["error"]["code"] == "NOT_CONNECTED" and not fixture[2].exists()
    assert result["source_unchanged"]


def test_report_failure_does_not_hide_source_and_cleanup_evidence(fixture, monkeypatch):
    def fail(*args):
        raise OSError("Output device failure")
    monkeypatch.setattr(study, "_write_reports", fail)
    result = run(fixture, parameters=[parameter([8])], dry_run=False)
    assert not result["ok"] and result["status"] == "failed"
    assert result["report_error"]["code"] == "STUDY_REPORT_WRITE_FAILED"
    assert result["source_unchanged"] and result["active_document_restored"]
    assert_source_untouched(fixture)


def test_public_contract_covers_parameters_and_reuses_design_assertions():
    tool = study.variant_tools()[0]
    validator = Draft202012Validator(tool.inputSchema)
    Draft202012Validator.check_schema(tool.inputSchema)
    valid = {"document_path": "C:/source.sldprt", "request_id": "study-1", "parameters": [parameter()],
             "assertions": [{"id": "mass", "kind": "mass", "unit": "kg", "max": .05, "abs_tolerance": 1e-6}]}
    assert not list(validator.iter_errors(valid))
    assert list(validator.iter_errors({**valid, "parameters": [parameter(values=[True])]}))
    assert tool.inputSchema["properties"]["dry_run"]["default"] is True
