"""Native health and detached repairs, using synthetic COM objects only."""
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.diagnostics import DiagnosticError, RepairPlans, diagnose
from solidworks_mcp_pro.validation import validate_result


class Feature:
    def __init__(self, name="Boss", kind="Extrusion", code=0, warning=False):
        self.Name, self.kind, self.code, self.warning = name, kind, code, warning
        self.next = self.sub = self.sub_next = None
        self.specific = None

    def GetTypeName2(self):
        return self.kind

    def GetErrorCode2(self):
        return self.code, self.warning

    def GetNextFeature(self):
        return self.next

    def GetFirstSubFeature(self):
        return self.sub

    def GetNextSubFeature(self):
        return self.sub_next

    def GetSpecificFeature2(self):
        return self.specific

    def IsSuppressed(self):
        return False


class Equations:
    def __init__(self):
        self.texts = []
        self.Status = 0

    def GetCount(self):
        return len(self.texts)

    def Equation(self, index):
        return self.texts[index]

    def GlobalVariable(self, index):
        return False

    def Value(self, index):
        self.Status = index
        return 10.0


class Component:
    def __init__(self, path, name="Bolt-1", state=2):
        self.path, self.Name2, self.state = path, name, state
        self.ReferencedConfiguration = "Default"
        self.IsVirtual = False
        self.sets = []
        self.model = None

    def GetPathName(self):
        return str(self.path)

    def GetSuppression2(self):
        return self.state

    def SetSuppression2(self, state):
        self.sets.append(state)
        self.state = state
        return 2

    def GetModelDoc2(self):
        return self.model


class Model:
    def __init__(self, path, kind=1):
        self.path, self.kind = path, kind
        self.stamp = 1
        self.config = "Default"
        self.dirty = self.read_only = self.view_only = False
        self.feature = Feature()
        self.equations = Equations()
        self.components = []
        self.rebuilds = 0
        self.timeout = False
        self.bodies = []
        self.editing_self = True
        self.sketch = None

    def GetPathName(self):
        return str(self.path)

    def GetType(self):
        return self.kind

    def GetActiveConfiguration(self):
        return SimpleNamespace(Name=self.config)

    def GetUpdateStamp(self):
        return self.stamp

    def GetSaveFlag(self):
        return self.dirty

    def IsOpenedReadOnly(self):
        return self.read_only

    def IsOpenedViewOnly(self):
        return self.view_only

    def IsEditingSelf(self):
        return self.editing_self

    def GetActiveSketch2(self):
        return self.sketch

    def FirstFeature(self):
        return self.feature

    def GetEquationMgr(self):
        return self.equations

    def Parameter(self, name):
        return None

    def GetComponents(self, top_only):
        assert top_only is False
        return tuple(self.components)

    def ForceRebuild3(self, top_only):
        self.rebuilds += 1
        self.stamp += 1
        if self.timeout:
            raise TimeoutError("native outcome unknown")
        return True

    def GetBodies2(self, kind, visible_only):
        assert (kind, visible_only) == (-1, False)
        return tuple(self.bodies)


class Session:
    def __init__(self, model):
        self._sw = SimpleNamespace(ActiveDoc=model)
        self.app = self._sw
        self.connects = 0

    def _require_model(self):
        raise AssertionError("Read-only health must never use document-activating helpers")

    def _ensure(self):
        self.connects += 1
        self._sw = self.app
        return self.app

    def _reattach_if_gone(self):
        pass


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "part.sldprt"
    path.write_bytes(b"saved native part")
    model = Model(path)
    return Session(model), model, path


def test_basic_scan_is_read_only_and_does_not_connect_or_rebuild(source):
    session, model, path = source
    before = path.read_bytes()
    result = diagnose(session, path)
    assert result["ok"] and result["healthy"]
    assert not result["mutation_attempted"] and not result["saved"]
    assert model.rebuilds == session.connects == 0 and path.read_bytes() == before
    assert result["counts"]["features"] == 1


def test_wrong_active_document_is_refused_without_activation(source, tmp_path):
    session, model, _ = source
    other = tmp_path / "other.sldprt"
    other.write_bytes(b"other")
    result = diagnose(session, other)
    assert not result["ok"] and not result["healthy"]
    assert result["errors"][0]["code"] == "DOCUMENT_MISMATCH"
    assert model.rebuilds == 0


def test_disconnected_scan_never_auto_connects(source):
    session, _, path = source
    session._sw = None
    result = diagnose(session, path)
    assert not result["ok"] and not result["healthy"] and session.connects == 0
    assert result["errors"][0]["code"] == "NOT_CONNECTED"


def test_mate_missing_reference_is_blocking_even_without_rebuild_failure(source):
    session, model, path = source
    model.kind = 2
    model.feature.kind = "MateGroup"
    mate = Feature("Concentric1", "MateConcentric")
    mate.specific = SimpleNamespace(GetMateEntityCount=lambda: 1,
                                    MateEntity=lambda index: SimpleNamespace(Reference=None))
    model.feature.sub = mate
    result = diagnose(session, path)
    assert result["ok"] and not result["healthy"]
    assert any(item["code"] == "MATE_REFERENCE_MISSING" for item in result["errors"])
    assert model.rebuilds == 0


def test_mate_warning_does_not_hide_a_missing_reference(source):
    session, model, path = source
    model.kind = 2
    model.feature.kind = "MateGroup"
    mate = Feature("Mate1", "MateCoincident", code=6, warning=True)
    mate.specific = SimpleNamespace(GetMateEntityCount=lambda: 1, MateEntity=lambda index: None)
    model.feature.sub = mate
    result = diagnose(session, path)
    assert not result["healthy"] and result["warnings"] and result["errors"]


def test_mate_reference_feature_is_not_miscast_as_an_assembly_constraint(source):
    session, model, path = source
    model.feature.kind = "MateReferenceGroup"
    model.feature.sub = Feature("MateReference1", "MateReference")
    assert diagnose(session, path)["healthy"]


def test_feature_warnings_and_underdefined_sketch_are_not_blocking(source):
    session, model, path = source
    model.feature.code, model.feature.warning = 19, True
    sketch = Feature("Sketch1", "ProfileFeature")
    sketch.specific = SimpleNamespace(GetConstrainedStatus=lambda: 2)
    model.feature.sub = sketch
    result = diagnose(session, path)
    assert result["healthy"] and len(result["warnings"]) == 2 and not result["errors"]


def test_unavailable_feature_state_never_reports_clean(source):
    session, model, path = source
    model.feature.GetErrorCode2 = lambda: (_ for _ in ()).throw(RuntimeError("COM read failed"))
    result = diagnose(session, path)
    assert result["ok"] and not result["healthy"]
    assert result["uninspected"][0]["scope"] == "features"


def test_missing_equation_and_component_references_are_reported(source, tmp_path):
    session, model, path = source
    model.kind = 2
    model.equations.texts = ['"D1@Deleted" = 10']
    model.components = [Component(tmp_path / "missing.sldprt")]
    result = diagnose(session, path)
    assert not result["healthy"]
    assert {item["code"] for item in result["errors"]} == {"EQUATION_ERROR", "REFERENCE_MISSING"}


def test_planning_is_detached_and_does_not_apply(source):
    session, model, path = source
    plans = RepairPlans()
    plan = plans.plan(session, path)
    assert plan["status"] == "planned" and plan["requires_apply"] and model.rebuilds == 0
    plan["steps"][0]["action"] = "delete_geometry"
    applied = plans.apply(session, plan["plan_id"])
    assert applied["ok"] and not applied["requires_apply"] and model.rebuilds == 1
    assert applied["steps"][0]["action"] == "rebuild"


@pytest.mark.parametrize("change", ["stamp", "config", "dirty", "file", "feature"])
def test_changed_preconditions_reject_before_any_repair(source, change):
    session, model, path = source
    plans = RepairPlans()
    plan = plans.plan(session, path)
    if change == "stamp":
        model.stamp += 1
    elif change == "config":
        model.config = "Other"
    elif change == "dirty":
        model.dirty = True
    elif change == "file":
        path.write_bytes(b"saved file replaced")
    else:
        model.feature.Name = "DifferentFeature"
    result = plans.apply(session, plan["plan_id"])
    assert not result["ok"] and result["code"] == "REPAIR_PLAN_STALE" and model.rebuilds == 0


def test_only_explicit_exact_lightweight_component_is_resolved(source, tmp_path):
    session, model, path = source
    model.kind = 2
    part = tmp_path / "bolt.sldprt"
    part.write_bytes(b"bolt")
    first, second = Component(part, "Bolt-1", 1), Component(part, "Bolt-2", 1)
    model.components = [first, second]
    plans = RepairPlans()
    plan = plans.plan(session, path, actions=["resolve", "rebuild"], components=["Bolt-1"])
    assert not first.sets and not second.sets
    result = plans.apply(session, plan["plan_id"])
    assert result["ok"] and first.sets == [3] and not second.sets
    assert model.rebuilds == 1


@pytest.mark.parametrize("actions,components", [([], None), (["delete"], None), (["rebuild"], ["Bolt-1"]),
                                                (["resolve"], []), (["resolve"], ["Bolt"])])
def test_invalid_actions_and_nonexact_component_targets_never_apply(source, actions, components):
    session, model, path = source
    with pytest.raises(DiagnosticError):
        RepairPlans().plan(session, path, actions=actions, components=components)
    assert model.rebuilds == 0


def test_timeout_is_retained_and_never_retried(source):
    session, model, path = source
    plans = RepairPlans()
    plan = plans.plan(session, path)
    model.timeout = True
    first = plans.apply(session, plan["plan_id"])
    second = plans.apply(session, plan["plan_id"])
    assert first["status"] == "indeterminate" and not first["ok"]
    assert second["replayed"] and model.rebuilds == 1


def test_reconnect_requires_plan_then_explicit_apply(source):
    session, _, path = source
    session._sw = None
    plans = RepairPlans()
    plan = plans.plan(session, path)
    assert plan["steps"] == [{"action": "reconnect"}] and session.connects == 0
    result = plans.apply(session, plan["plan_id"])
    assert result["ok"] and session.connects == 1


def test_stale_com_connection_can_only_plan_a_standalone_reconnect(source):
    session, _, path = source

    class Gone:
        @property
        def ActiveDoc(self):
            raise RuntimeError("RPC server disconnected")

    session._sw = Gone()
    plans = RepairPlans()
    plan = plans.plan(session, path)
    assert plan["steps"] == [{"action": "reconnect"}]
    assert plans.apply(session, plan["plan_id"])["ok"] and session.connects == 1


def test_scan_cannot_report_clean_when_document_changes_mid_scan(source):
    session, model, path = source
    original = model.GetEquationMgr

    def changed():
        model.stamp += 1
        return original()

    model.GetEquationMgr = changed
    result = diagnose(session, path)
    assert not result["healthy"]
    assert any(item["code"] == "DOCUMENT_CHANGED_DURING_SCAN" for item in result["uninspected"])


def test_readonly_document_is_inspectable_but_cannot_receive_repair_plan(source):
    session, model, path = source
    model.read_only = True
    assert diagnose(session, path)["healthy"]
    with pytest.raises(DiagnosticError, match="DOCUMENT_READ_ONLY"):
        RepairPlans().plan(session, path)


@pytest.mark.parametrize("edit_mode", ["sketch", "in_context"])
def test_active_edit_is_reported_without_exiting_it_and_repair_is_refused(source, edit_mode):
    session, model, path = source
    if edit_mode == "sketch":
        model.sketch = object()
    else:
        model.editing_self = False
    result = diagnose(session, path)
    assert not result["healthy"] and result["errors"][0]["code"] == "DOCUMENT_EDIT_MODE"
    with pytest.raises(DiagnosticError, match="DOCUMENT_EDIT_MODE"):
        RepairPlans().plan(session, path)
    assert model.rebuilds == session.connects == 0


def test_deep_body_check_is_native_and_faults_block_render(source):
    session, model, path = source
    model.bodies = [SimpleNamespace(Check3=SimpleNamespace(Count=2))]
    result = diagnose(session, path, deep=True)
    assert result["ok"] and not result["healthy"] and result["counts"]["bodies_checked"] == 1
    assert result["errors"][0]["code"] == "BODY_GEOMETRY_ERROR" and model.rebuilds == 0


def test_deep_unsupported_geometry_never_reports_clean(source):
    session, model, path = source
    model.bodies = [object()]
    result = diagnose(session, path, deep=True)
    assert not result["healthy"] and result["uninspected"][0]["scope"] == "geometry"


@pytest.mark.parametrize("fail", [False, True])
def test_deep_interference_restores_every_setting_even_after_native_failure(source, fail):
    session, model, path = source
    model.kind = 2

    class Manager:
        TreatCoincidenceAsInterference = True
        TreatSubAssembliesAsComponents = True
        IncludeMultibodyPartInterferences = False
        done = False

        def GetInterferences(self):
            if fail:
                raise RuntimeError("interference failed")
            return [SimpleNamespace(Components=[], Volume=1e-9)]

        def Done(self):
            self.done = True

    manager = Manager()
    model.InterferenceDetectionManager = manager
    result = diagnose(session, path, deep=True)
    assert not result["healthy"] and manager.done
    assert (manager.TreatCoincidenceAsInterference, manager.TreatSubAssembliesAsComponents,
            manager.IncludeMultibodyPartInterferences) == (True, True, False)
    if fail:
        assert any(item["scope"] == "interference" for item in result["uninspected"])
    else:
        assert result["counts"]["interferences"] == 1


def test_deep_interference_does_not_assign_options_that_already_match(source):
    session, model, path = source
    model.kind = 2

    class Manager:
        TreatCoincidenceAsInterference = False
        TreatSubAssembliesAsComponents = False
        IncludeMultibodyPartInterferences = True

        def __setattr__(self, name, value):
            raise AssertionError("No setter is needed for an already matching native option")

        def GetInterferences(self):
            return ()

        def Done(self):
            pass

    model.InterferenceDetectionManager = Manager()
    result = diagnose(session, path, deep=True)
    assert result["healthy"]
    assert result["native_checks"]["interference"]["settings_changed"] == []
    assert result["document_states"]["before"] == result["document_states"]["final"]


@pytest.mark.parametrize("phase,stamp_change,expected_healthy", [
    ("done", False, True), ("done", True, False), ("query", False, False),
])
def test_deep_done_dirty_only_effect_is_explicit_and_other_changes_stay_blocking(
        source, phase, stamp_change, expected_healthy):
    session, model, path = source
    model.kind = 2

    class Manager:
        TreatCoincidenceAsInterference = False
        TreatSubAssembliesAsComponents = False
        IncludeMultibodyPartInterferences = True

        def GetInterferences(self):
            if phase == "query":
                model.dirty = True
            return ()

        def Done(self):
            if phase == "done":
                model.dirty = True
                if stamp_change:
                    model.stamp += 1

    model.InterferenceDetectionManager = Manager()
    result = diagnose(session, path, deep=True)
    assert result["healthy"] is expected_healthy and model.dirty is True
    assert result["document_states"]["before"]["dirty"] is False
    assert result["document_states"]["final"]["dirty"] is True
    if expected_healthy:
        assert result["warnings"][0]["code"] == "DEEP_CHECK_MARKED_DIRTY"
        assert result["model_state_changed"] and not result["saved"]
    else:
        assert result["uninspected"][0]["code"] == "DOCUMENT_CHANGED_DURING_SCAN"


@pytest.mark.parametrize("field", ["failing_features", "mate_errors", "broken_equations"])
def test_native_postconditions_stop_on_explicit_errors_with_successful_rebuild(field):
    source = {"ok": True, "rebuild_ok": True, field: [{"name": "Broken", "warning": False}]}
    result = validate_result(source)
    assert not result["ok"] and result["error_code"] == "CAD_POSTCONDITION_FAILED"
    assert source["ok"] and result[field] == source[field]


def test_native_warning_only_mates_do_not_stop_plans():
    source = {"ok": True, "rebuild_ok": True,
              "mate_errors": [{"name": "Mate1", "error": {"code": 5, "warning": True}}],
              "failing_features": [{"name": "Fillet1", "warning": True}]}
    assert validate_result(source)["ok"]
