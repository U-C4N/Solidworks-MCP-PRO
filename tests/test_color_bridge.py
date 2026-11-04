from pathlib import Path
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import color_bridge as bridge


BASE = [0.2, 0.3, 0.4, 0.8, 0.7, 0.6, 0.5, 0.1, 0.0]
ABSENT = [-1.0] * 9


class Component:
    def __init__(self, name, values=None):
        self.Name2 = name
        self.values = list(BASE if values is None else values)
        self.inherited = SimpleNamespace(MaterialPropertyValues=list(BASE))
        self.writes = []
        self.removals = 0
        self.suppressed = False
        self.fail_writes = set()
        self.mismatch_writes = set()
        self.fail_remove = False

    def GetMaterialPropertyValues2(self, scope, names):
        assert (scope, names) == (1, None)
        return tuple(self.values)

    def SetMaterialPropertyValues2(self, values, scope, names):
        assert (scope, names) == (1, None)
        self.writes.append(list(values))
        self.values = list(values)
        if len(self.writes) in self.mismatch_writes:
            self.values[0] = (self.values[0] + 0.25) % 1.0
        if len(self.writes) in self.fail_writes:
            raise RuntimeError("setter failed after a partial write")
        return True

    def RemoveMaterialProperty2(self, scope, names):
        assert (scope, names) == (1, None)
        self.removals += 1
        if self.fail_remove:
            return False
        self.values = ABSENT[:]
        return True

    def GetModelDoc2(self):
        return self.inherited

    def IsSuppressed(self):
        return self.suppressed


class Model:
    def __init__(self, path, doc_type=2):
        self.path = str(path)
        self.doc_type = doc_type
        self.read_only = self.view_only = False
        self.components = [Component("Frame-1"), Component("Handle-1", ABSENT)]
        self.values = BASE[:]
        self.writes = []
        self.redraws = 0
        self.removals = 0
        self.mismatch_writes = set()
        self.Extension = SimpleNamespace(RemoveMaterialProperty=self.remove)

    def GetPathName(self):
        return self.path

    def GetType(self):
        return self.doc_type

    def IsOpenedReadOnly(self):
        return self.read_only

    def IsOpenedViewOnly(self):
        return self.view_only

    def GetComponents(self, top_only):
        assert top_only is True
        return self.components

    def GraphicsRedraw2(self):
        self.redraws += 1

    @property
    def MaterialPropertyValues(self):
        return tuple(self.values)

    @MaterialPropertyValues.setter
    def MaterialPropertyValues(self, values):
        self.writes.append(list(values))
        self.values = list(values)
        if len(self.writes) in self.mismatch_writes:
            self.values[0] = (self.values[0] + 0.25) % 1.0

    def remove(self, scope, names):
        assert (scope, names) == (1, None)
        self.removals += 1
        self.values = ABSENT[:]
        return True


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "colors.sldasm"
    path.write_bytes(b"assembly fixture")
    model = Model(path)
    app = SimpleNamespace(ActiveDoc=model)
    assignments = [{"component": "Frame-1", "color_rgb": [10, 20, 30]},
                   {"component": "Handle-1", "color_rgb": [90, 100, 110]}]
    return app, path, assignments


def assert_unmutated(model):
    assert model.redraws == 0 and model.writes == [] and model.removals == 0
    assert all(component.writes == [] and component.removals == 0 for component in model.components)


def test_batch_uses_one_redraw_preserves_coefficients_and_reads_each_target(setup):
    app, path, assignments = setup
    model = app.ActiveDoc
    result = bridge.colorize(app, path, assignments)
    assert result["ok"] and result["verified"] and result["mutation_attempted"]
    assert result["changed_count"] == 2 and result["skipped_count"] == 0
    assert result["saved"] is result["rebuilt"] is False
    assert result["configuration_scope"] == "current"
    assert model.redraws == 1
    for component, assignment, state in zip(model.components, assignments, result["after"]):
        assert state["color_rgb"] == assignment["color_rgb"]
        assert component.values[3:] == BASE[3:]
        assert state["has_override"]
    assert result["before"][1]["has_override"] is False
    assert result["before"][1]["values"] == ABSENT


@pytest.mark.parametrize("change", [
    {"component": "missing"}, {"component": None}, {"component": ""},
    {"component": "Outer-1/Child-1"}, {"component": "Outer-1\\Child-1"},
    {"color_rgb": None}, {"color_rgb": [True, 0, 0]}, {"color_rgb": [1.0, 0, 0]},
    {"color_rgb": [256, 0, 0]}, {"color_rgb": [-1, 0, 0]}, {"color_rgb": [0, 1]},
    {"color_rgb": [float("nan"), 0, 0]}, {"extra": 1},
])
def test_invalid_later_assignment_never_writes_earlier_target(setup, change):
    app, path, assignments = setup
    assignments[1].update(change)
    with pytest.raises(bridge.ColorError):
        bridge.colorize(app, path, assignments)
    assert_unmutated(app.ActiveDoc)


@pytest.mark.parametrize("assignments", [[], None, [{}], [{"color_rgb": [0, 0, 0]}] * 129])
def test_invalid_batch_rejected_before_mutation(setup, assignments):
    app, path, _ = setup
    with pytest.raises(bridge.ColorError):
        bridge.colorize(app, path, assignments)
    assert_unmutated(app.ActiveDoc)


def test_duplicate_requested_or_native_targets_are_rejected(setup):
    app, path, assignments = setup
    with pytest.raises(bridge.ColorError, match="unique"):
        bridge.colorize(app, path, [assignments[0], assignments[0]])
    app.ActiveDoc.components.append(Component("Frame-1"))
    with pytest.raises(bridge.ColorError, match="COMPONENT_NOT_UNIQUE"):
        bridge.colorize(app, path, assignments)
    assert_unmutated(app.ActiveDoc)


def test_suppressed_target_and_malformed_snapshot_are_preflight_failures(setup):
    app, path, assignments = setup
    app.ActiveDoc.components[1].suppressed = True
    with pytest.raises(bridge.ColorError, match="COMPONENT_UNAVAILABLE"):
        bridge.colorize(app, path, assignments)
    app.ActiveDoc.components[1].suppressed = False
    app.ActiveDoc.components[1].values = [0.1] * 8
    with pytest.raises(bridge.ColorError, match="COLOR_PREFLIGHT_FAILED"):
        bridge.colorize(app, path, assignments)
    assert_unmutated(app.ActiveDoc)


@pytest.mark.parametrize("flag", ["read_only", "view_only"])
def test_readonly_mutation_rejected_but_dry_run_reads_snapshots(setup, flag):
    app, path, assignments = setup
    setattr(app.ActiveDoc, flag, True)
    with pytest.raises(bridge.ColorError, match="DOCUMENT_READ_ONLY"):
        bridge.colorize(app, path, assignments)
    result = bridge.colorize(app, path, assignments, dry_run=True)
    assert result["ok"] and result["dry_run"] and not result["mutation_attempted"]
    assert len(result["before"]) == 2
    assert_unmutated(app.ActiveDoc)


@pytest.mark.parametrize("dry_run", [None, 1, "true"])
def test_dry_run_requires_boolean(setup, dry_run):
    app, path, assignments = setup
    with pytest.raises(bridge.ColorError, match="INVALID_ARGUMENT"):
        bridge.colorize(app, path, assignments, dry_run=dry_run)
    assert_unmutated(app.ActiveDoc)


def test_exact_active_saved_path_required(setup, tmp_path):
    app, path, assignments = setup
    other = tmp_path / "other.sldasm"
    other.write_bytes(b"other fixture")
    with pytest.raises(bridge.ColorError, match="DOCUMENT_MISMATCH"):
        bridge.colorize(app, other, assignments)
    app.ActiveDoc.path = ""
    with pytest.raises(bridge.ColorError, match="DOCUMENT_MISMATCH"):
        bridge.colorize(app, path, assignments)
    app.ActiveDoc.path = str(tmp_path / "no-longer-present.sldasm")
    with pytest.raises(bridge.ColorError, match="DOCUMENT_MISMATCH"):
        bridge.colorize(app, path, assignments)
    assert_unmutated(app.ActiveDoc)


def test_relative_nonexistent_and_wrong_suffix_paths_rejected(setup, tmp_path):
    app, _, assignments = setup
    invalid = tmp_path / "not-cad.txt"
    invalid.write_bytes(b"fixture")
    for path in [Path("colors.sldasm"), tmp_path / "missing.sldasm", invalid, "", None]:
        with pytest.raises(bridge.ColorError):
            bridge.colorize(app, path, assignments)
    assert_unmutated(app.ActiveDoc)


def test_part_scope_is_whole_part_only_and_preserves_native_coefficients(tmp_path):
    path = tmp_path / "part.sldprt"
    path.write_bytes(b"part fixture")
    model = Model(path, 1)
    app = SimpleNamespace(ActiveDoc=model)
    with pytest.raises(bridge.ColorError, match="PART_COMPONENT"):
        bridge.colorize(app, path, [{"component": "Frame-1", "color_rgb": [10, 20, 30]}])
    result = bridge.colorize(app, path, [{"color_rgb": [10, 20, 30]}])
    assert result["ok"] and result["after"][0]["color_rgb"] == [10, 20, 30]
    assert model.values[3:] == BASE[3:] and model.redraws == 1
    assert all(not component.writes for component in model.components)


def test_assembly_requires_component_and_does_not_change_referenced_part(setup):
    app, path, assignments = setup
    with pytest.raises(bridge.ColorError, match="COMPONENT_REQUIRED"):
        bridge.colorize(app, path, [{"color_rgb": [10, 20, 30]}])
    assert_unmutated(app.ActiveDoc)
    assert bridge.colorize(app, path, assignments)["ok"]
    assert all(component.inherited.MaterialPropertyValues == BASE for component in app.ActiveDoc.components)


def test_verified_noop_skips_native_writes_and_redraw(setup):
    app, path, assignments = setup
    for component, assignment in zip(app.ActiveDoc.components, assignments):
        component.values = [channel / 255 for channel in assignment["color_rgb"]] + BASE[3:]
    result = bridge.colorize(app, path, assignments)
    assert result["ok"] and result["verified"] and not result["mutation_attempted"]
    assert result["changed_count"] == 0 and result["skipped_count"] == 2
    assert_unmutated(app.ActiveDoc)


def test_inherited_unresolved_component_uses_default_coefficients(setup):
    app, path, assignments = setup
    app.ActiveDoc.components[1].inherited = None
    result = bridge.colorize(app, path, assignments)
    assert result["ok"]
    assert result["after"][1]["values"][3:] == bridge._DEFAULT[3:]


def test_mid_batch_exception_restores_existing_values_and_absence_of_override(setup):
    app, path, assignments = setup
    app.ActiveDoc.components[1].fail_writes = {1}
    result = bridge.colorize(app, path, assignments)
    assert result["ok"] is False and result["code"] == "COLOR_APPLY_FAILED"
    assert result["failed_stage"] == "apply:Handle-1"
    assert result["rollback"]["attempted"] and result["rollback"]["verified"]
    assert not result["partial_state_possible"] and not result["outcome_may_be_indeterminate"]
    assert app.ActiveDoc.components[0].values == BASE
    assert app.ActiveDoc.components[1].values == ABSENT
    assert app.ActiveDoc.components[1].removals == 1
    assert app.ActiveDoc.redraws == 1
    assert result["saved"] is result["rebuilt"] is False


def test_readback_mismatch_fails_and_verifies_rollback(setup):
    app, path, assignments = setup
    app.ActiveDoc.components[0].mismatch_writes = {1}
    result = bridge.colorize(app, path, assignments)
    assert result["ok"] is False and result["failed_stage"] == "readback:Frame-1"
    assert result["rollback"]["verified"] and app.ActiveDoc.components[0].values == BASE
    assert not app.ActiveDoc.components[1].writes


def test_one_rgb_channel_difference_is_not_accepted_as_rounding(setup):
    app, path, assignments = setup
    component = app.ActiveDoc.components[0]
    original_setter = component.SetMaterialPropertyValues2

    def wrong_channel(values, scope, names):
        accepted = original_setter(values, scope, names)
        if len(component.writes) == 1:
            component.values[0] += 1 / 255
        return accepted

    component.SetMaterialPropertyValues2 = wrong_channel
    result = bridge.colorize(app, path, assignments)
    assert not result["ok"] and result["rollback"]["verified"]


def test_failure_to_remove_previously_absent_override_reports_partial_unknown_state(setup):
    app, path, assignments = setup
    component = app.ActiveDoc.components[1]
    component.fail_writes = {1}
    component.fail_remove = True
    result = bridge.colorize(app, path, assignments)
    assert not result["ok"] and not result["rollback"]["verified"]
    assert result["partial_state_possible"] and result["outcome_may_be_indeterminate"]
    assert any("removal returned false" in error for error in result["rollback"]["errors"])
    assert app.ActiveDoc.components[0].values == BASE and component.values != ABSENT


def test_silent_rollback_mismatch_never_claims_restore(setup):
    app, path, assignments = setup
    app.ActiveDoc.components[0].mismatch_writes = {2}
    app.ActiveDoc.components[1].fail_writes = {1}
    result = bridge.colorize(app, path, assignments)
    assert not result["ok"] and not result["rollback"]["verified"]
    assert result["partial_state_possible"]
    assert any("rollback appearance readback differs" in error for error in result["rollback"]["errors"])


def test_part_failure_restores_no_prior_override_with_extension_remove(tmp_path):
    path = tmp_path / "part.sldprt"
    path.write_bytes(b"part fixture")
    model = Model(path, 1)
    model.values = ABSENT[:]
    model.mismatch_writes = {1}
    result = bridge.colorize(SimpleNamespace(ActiveDoc=model), path, [{"color_rgb": [10, 20, 30]}])
    assert not result["ok"] and result["rollback"]["verified"]
    assert model.values == ABSENT and model.removals == 1 and model.redraws == 1


def test_com_property_values_are_not_called_as_default_members(setup):
    app, path, assignments = setup

    class CallableModel(Model):
        def __call__(self):
            pytest.fail("ActiveDoc is a property; do not invoke its returned dispatch")

    app.ActiveDoc = CallableModel(path)
    assert bridge.colorize(app, path, assignments)["ok"]


def test_native_false_return_is_failure_even_if_readback_might_match(setup):
    app, path, assignments = setup
    component = app.ActiveDoc.components[0]
    original_setter = component.SetMaterialPropertyValues2

    def rejected(values, scope, names):
        original_setter(values, scope, names)
        return len(component.writes) != 1

    component.SetMaterialPropertyValues2 = rejected
    result = bridge.colorize(app, path, assignments)
    assert not result["ok"] and result["rollback"]["verified"]
    assert "returned false" in result["error"]
    assert component.values == BASE and not app.ActiveDoc.components[1].writes


def test_document_change_after_setter_fails_and_restores_original_target(setup, tmp_path):
    app, path, assignments = setup
    original = app.ActiveDoc
    other_path = tmp_path / "other.sldasm"
    other_path.write_bytes(b"other fixture")
    replacement = Model(other_path)

    def redraw():
        original.redraws += 1
        app.ActiveDoc = replacement

    original.GraphicsRedraw2 = redraw
    result = bridge.colorize(app, path, assignments)
    assert not result["ok"] and result["rollback"]["verified"]
    assert result["failed_stage"] == "final_readback"
    assert original.components[0].values == BASE and original.components[1].values == ABSENT
    assert_unmutated(replacement)


def test_redraw_failure_rolls_back_and_does_not_claim_success(setup):
    app, path, assignments = setup
    model = app.ActiveDoc

    def failed_redraw():
        model.redraws += 1
        raise RuntimeError("graphics unavailable")

    model.GraphicsRedraw2 = failed_redraw
    result = bridge.colorize(app, path, assignments)
    assert not result["ok"] and result["failed_stage"] == "redraw"
    assert result["rollback"]["verified"]
    assert any("rollback redraw" in item for item in result["rollback"]["errors"])
    assert model.components[0].values == BASE and model.components[1].values == ABSENT
