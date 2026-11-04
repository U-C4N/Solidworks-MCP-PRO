"""Independent transaction acceptance tests with stateful native-interface fakes.

The fake undo stack belongs to the model, not to NativeSession. This catches
incorrect undo claims, incomplete restoration and execution against a new target
without opening SOLIDWORKS or importing an external CAD engine.
"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.native.errors import CadError
from solidworks_mcp_pro.native import binding
from solidworks_mcp_pro.native.session import NativeSession, same_object


class Feature:
    def __init__(self, model, name, *, error=0, warning=False):
        self.model, self.Name = model, name
        self.error, self.warning = error, warning

    def GetTypeName2(self):
        return "Boss"

    def GetErrorCode2(self):
        return self.error, self.warning

    def GetNextFeature(self):
        index = self.model.features.index(self) + 1
        return self.model.features[index] if index < len(self.model.features) else None

    def GetFirstSubFeature(self):
        return None


class Selection:
    def __init__(self):
        self.items = []
        self.reject_add = False
        self.reject_mark = False

    def GetSelectedObjectCount2(self, mark):
        assert mark == -1
        return len(self.items)

    def GetSelectedObject6(self, index, mark):
        assert mark == -1
        return self.items[index - 1][0]

    def GetSelectedObjectMark(self, index):
        return self.items[index - 1][1]

    def AddSelectionListObject(self, entity, data):
        if self.reject_add:
            return 0
        self.items.append((entity, 0))
        return len(self.items)

    def SetSelectedObjectMark(self, index, mark, action):
        assert action == 0
        if self.reject_mark:
            return False
        self.items[index - 1] = self.items[index - 1][0], mark
        return True


class Model:
    def __init__(self):
        self.features = [Feature(self, "Base")]
        self.volume_mm3 = 1250.
        self.has_solids = True
        self.configuration = "Default"
        self.native_parameter_mm = 10.
        self.read_only = self.view_only = False
        self.active_sketch = None
        self.editing_self = True
        self.ActiveView = SimpleNamespace(EnableGraphicsUpdate=True)
        self.FeatureManager = SimpleNamespace(EnableFeatureTree=True)
        self.SelectionManager = Selection()
        self.Extension = UndoExtension(self)
        self.undo_count, self.clear_count = 0, 0
        self.undo_mode = "restore"

    def capture(self):
        return {"features": [(item.Name, item.error, item.warning) for item in self.features],
                "volume_mm3": self.volume_mm3, "configuration": self.configuration,
                "native_parameter_mm": self.native_parameter_mm}

    def restore(self, state):
        self.features = [Feature(self, name, error=error, warning=warning)
                         for name, error, warning in state["features"]]
        self.volume_mm3 = state["volume_mm3"]
        self.configuration = state["configuration"]
        self.native_parameter_mm = state["native_parameter_mm"]

    def EditUndo2(self, count):
        assert count == 1
        self.undo_count += 1
        if self.undo_mode == "throw":
            raise RuntimeError("native undo refused")
        if self.undo_mode == "restore":
            self.restore(self.Extension.units.pop())

    def GetType(self):
        return 1

    def GetBodies2(self, kind, visible_only):
        assert kind == 0 and visible_only is False
        return [self] if self.has_solids else []

    def GetTitle(self):
        return "Native test part"

    def GetPathName(self):
        return ""

    def GetSaveFlag(self):
        return True

    def GetUpdateStamp(self):
        return len(self.features)

    def IsOpenedReadOnly(self):
        return self.read_only

    def IsOpenedViewOnly(self):
        return self.view_only

    def GetActiveSketch2(self):
        return self.active_sketch

    def IsEditingSelf(self):
        return self.editing_self

    def FirstFeature(self):
        return self.features[0] if self.features else None

    def GetActiveConfiguration(self):
        return SimpleNamespace(Name=self.configuration)

    def ClearSelection2(self, all_marks):
        assert all_marks is True
        self.clear_count += 1
        self.SelectionManager.items.clear()


class UndoExtension:
    def __init__(self, model):
        self.model, self.units = model, []
        self.start_count = self.finish_count = 0
        self.finish_result = True
        self.start_error = self.finish_error = None
        self.pending = None

    def StartRecordingUndoObject(self):
        self.start_count += 1
        if self.start_error:
            raise self.start_error
        self.pending = self.model.capture()

    def FinishRecordingUndoObject2(self, label, hidden):
        self.finish_count += 1
        assert label and hidden is False
        if self.finish_error:
            raise self.finish_error
        if self.finish_result:
            self.units.append(self.pending)
        self.pending = None
        return self.finish_result

    def CreateMassProperty(self):
        if self.model.volume_mm3 is None:
            return None
        volume = self.model.volume_mm3 / 1e9
        return SimpleNamespace(UseSystemUnits=False, Volume=volume, Density=7800., Mass=volume * 7800.)


class App:
    def __init__(self, model):
        self.ActiveDoc = model
        self.CommandInProgress = False


@pytest.fixture
def cad():
    model = Model()
    app = App(model)
    session = NativeSession(app)
    session._model = model
    session.remember(model, editable=True, opened=True)
    return session, model, app


def mutate(model):
    model.features.append(Feature(model, "New boss"))
    model.volume_mm3 += 100.
    model.native_parameter_mm = 20.
    model.SelectionManager.items[:] = [(object(), 99)]


def test_failure_uses_native_undo_and_restores_selection_and_ui(cad):
    session, model, app = cad
    selected = [(object(), 2), (object(), 16)]
    model.SelectionManager.items[:] = selected
    baseline = model.capture()
    session._topology_cache = {"obsolete": object()}

    def create_then_fail():
        assert app.CommandInProgress is True
        assert model.ActiveView.EnableGraphicsUpdate is False
        assert model.FeatureManager.EnableFeatureTree is False
        mutate(model)
        raise CadError("EXPECTED_NATIVE_REJECTION", "The kernel rejected this feature.")

    with pytest.raises(CadError) as caught:
        session.run_guarded(create_then_fail)

    assert caught.value.code == "EXPECTED_NATIVE_REJECTION"
    assert model.capture() == baseline
    assert model.undo_count == 1
    assert model.Extension.start_count == model.Extension.finish_count == 1
    assert model.SelectionManager.items == selected
    assert model.ActiveView.EnableGraphicsUpdate is True
    assert model.FeatureManager.EnableFeatureTree is True
    assert app.CommandInProgress is False
    assert not session._topology_cache
    receipt = caught.value.details["native_receipt"]
    assert receipt is session.last_receipt
    assert receipt["rollback"]["attempted"] is True
    assert receipt["rollback"]["verified"] is True
    assert "scope" in receipt["rollback"]


def test_success_records_one_unit_and_preserves_preexisting_disabled_ui(cad):
    session, model, app = cad
    app.CommandInProgress = True
    model.ActiveView.EnableGraphicsUpdate = False
    selected = [(object(), 4)]
    model.SelectionManager.items[:] = selected

    def create():
        mutate(model)
        return {"ok": True, "feature": "New boss"}

    result = session.run_guarded(create)
    assert result["ok"] is True
    assert [item.Name for item in model.features] == ["Base", "New boss"]
    assert model.undo_count == 0
    assert len(model.Extension.units) == 1
    assert model.SelectionManager.items == selected
    assert app.CommandInProgress is True
    assert model.ActiveView.EnableGraphicsUpdate is False
    assert model.FeatureManager.EnableFeatureTree is True
    assert result["native_receipt"]["rollback"]["attempted"] is False


@pytest.mark.parametrize("answer", [None, False, {"ok": False}, {"ok": 1}])
def test_unverified_handler_result_is_an_error_and_native_state_is_undone(cad, answer):
    session, model, _ = cad
    baseline = model.capture()

    def unverified():
        mutate(model)
        return answer

    with pytest.raises(CadError) as caught:
        session.run_guarded(unverified)
    assert caught.value.code == "NATIVE_OPERATION_FAILED"
    assert model.capture() == baseline
    assert model.undo_count == 1


@pytest.mark.parametrize("undo_mode", ["throw", "no_op"])
def test_refused_or_ineffective_undo_cannot_be_claimed_verified(cad, undo_mode):
    session, model, app = cad
    model.undo_mode = undo_mode

    def failing():
        mutate(model)
        raise RuntimeError("failed after native mutation")

    with pytest.raises(CadError) as caught:
        session.run_guarded(failing)
    error = caught.value
    assert error.details["native_receipt"]["rollback"]["verified"] is False
    assert error.details["outcome_may_be_indeterminate"] is True
    assert model.ActiveView.EnableGraphicsUpdate is True
    assert model.FeatureManager.EnableFeatureTree is True
    assert app.CommandInProgress is False


def test_unknown_volume_does_not_upgrade_undo_into_verified_model_restoration(cad):
    session, model, _ = cad
    model.volume_mm3 = None

    def failing():
        model.native_parameter_mm = 20.
        raise RuntimeError("geometry measurement is unavailable")

    with pytest.raises(CadError) as caught:
        session.run_guarded(failing)
    rollback = caught.value.details["native_receipt"]["rollback"]
    assert rollback["attempted"] is True
    assert rollback.get("verified") is not True
    assert caught.value.details["outcome_may_be_indeterminate"] is True


def test_native_finish_refusal_reports_missing_undo_assurance(cad):
    session, model, app = cad
    model.Extension.finish_result = False

    def create():
        mutate(model)
        return {"ok": True}

    result = session.run_guarded(create)
    assert result["ok"] is True
    receipt = result["native_receipt"]
    assert receipt["undo_recorded"] is False
    assert receipt["warning"]
    assert receipt["rollback"]["attempted"] is False
    assert receipt["rollback"].get("verified") is not True
    assert model.undo_count == 0
    assert model.ActiveView.EnableGraphicsUpdate is True
    assert app.CommandInProgress is False


def test_native_failure_without_an_undo_unit_is_explicitly_indeterminate(cad):
    session, model, app = cad
    model.Extension.finish_result = False

    def failing():
        mutate(model)
        raise RuntimeError("native rejection without undo support")

    with pytest.raises(CadError) as caught:
        session.run_guarded(failing)
    receipt = caught.value.details["native_receipt"]
    assert receipt["undo_recorded"] is False
    assert receipt["rollback"]["attempted"] is False
    assert caught.value.details["outcome_may_be_indeterminate"] is True
    assert model.undo_count == 0
    assert app.CommandInProgress is False


@pytest.mark.parametrize("phase", ["start", "finish"])
def test_recording_exceptions_still_restore_ui_command_and_selection(cad, phase):
    session, model, app = cad
    selected = [(object(), 8)]
    model.SelectionManager.items[:] = selected
    setattr(model.Extension, phase + "_error", RuntimeError("native undo API failed"))

    def create():
        mutate(model)
        return {"ok": True}

    with pytest.raises(CadError) as caught:
        session.run_guarded(create)
    assert caught.value.details["native_receipt"]["rollback"]["attempted"] is False
    assert caught.value.details["outcome_may_be_indeterminate"] is True
    assert model.SelectionManager.items == selected
    assert model.ActiveView.EnableGraphicsUpdate is True
    assert model.FeatureManager.EnableFeatureTree is True
    assert app.CommandInProgress is False


def test_existing_healthy_feature_becoming_broken_is_a_postcondition_failure(cad):
    session, model, _ = cad

    def damage_existing_feature():
        model.features[0].error = 71
        return {"ok": True}

    with pytest.raises(CadError) as caught:
        session.run_guarded(damage_existing_feature)
    assert caught.value.code == "CAD_POSTCONDITION_FAILED"
    assert model.features[0].error == 0
    assert model.undo_count == 1


def test_preexisting_error_does_not_prevent_unrelated_success(cad):
    session, model, _ = cad
    model.features[0].error = 71

    def create():
        mutate(model)
        return {"ok": True}

    assert session.run_guarded(create)["ok"] is True
    assert model.features[0].error == 71


def test_native_warning_is_not_a_failed_feature(cad):
    session, model, _ = cad

    def create():
        model.features.append(Feature(model, "Warned", error=71, warning=True))
        return {"ok": True}

    assert session.run_guarded(create)["ok"] is True


def test_undo_receipt_rejects_observed_feature_errors_left_behind(cad):
    session, model, _ = cad
    model.undo_mode = "no_op"

    def damage_and_fail():
        model.features[0].error = 72
        raise RuntimeError("feature failed without a name or volume change")

    with pytest.raises(CadError) as caught:
        session.run_guarded(damage_and_fail)
    assert model.features[0].error == 72
    assert caught.value.details["native_receipt"]["rollback"]["verified"] is False
    assert caught.value.details["outcome_may_be_indeterminate"] is True


def test_selection_mark_restoration_rejection_is_not_reported_as_success(cad):
    session, model, app = cad
    model.SelectionManager.items = [(object(), 16)]

    def create():
        model.SelectionManager.reject_mark = True
        return {"ok": True}

    with pytest.raises(CadError):
        session.run_guarded(create)
    assert app.CommandInProgress is False


def test_selection_restoration_failure_does_not_skip_command_cleanup(cad):
    session, model, app = cad
    model.SelectionManager.items = [(object(), 16)]

    def create():
        model.SelectionManager.reject_add = True
        return {"ok": True}

    with pytest.raises(CadError) as caught:
        session.run_guarded(create)
    assert "selection_restoration_error" in caught.value.details["native_receipt"]
    assert app.CommandInProgress is False
    assert model.ActiveView.EnableGraphicsUpdate is True
    assert model.FeatureManager.EnableFeatureTree is True


class TreeRestorationFailure:
    def __init__(self):
        self.enabled = True

    @property
    def EnableFeatureTree(self):
        return self.enabled

    @EnableFeatureTree.setter
    def EnableFeatureTree(self, value):
        if value:
            raise RuntimeError("tree restoration refused")
        self.enabled = value


class DisableFailure:
    def __init__(self):
        self.enabled = True

    @property
    def EnableFeatureTree(self):
        return self.enabled

    @EnableFeatureTree.setter
    def EnableFeatureTree(self, value):
        self.enabled = value
        if not value:
            raise RuntimeError("native setter failed after disabling tree")


def test_failed_ui_disable_still_restores_control_changed_by_throwing_setter(cad):
    session, model, app = cad
    model.FeatureManager = DisableFailure()
    called = []

    with pytest.raises(CadError):
        session.run_guarded(lambda: called.append(True))
    assert called == []
    assert model.FeatureManager.EnableFeatureTree is True
    assert model.ActiveView.EnableGraphicsUpdate is True
    assert app.CommandInProgress is False


def test_ui_restore_failure_still_undoes_native_mutation_and_restores_other_ui(cad):
    session, model, app = cad
    model.FeatureManager = TreeRestorationFailure()
    baseline = model.capture()

    def create():
        mutate(model)
        return {"ok": True}

    with pytest.raises(CadError) as caught:
        session.run_guarded(create)
    assert caught.value.code == "UI_RESTORATION_FAILED"
    assert model.capture() == baseline
    assert model.ActiveView.EnableGraphicsUpdate is True
    assert app.CommandInProgress is False


class CommandStartFailure(App):
    def __init__(self, model):
        self.ActiveDoc = model
        self.command = False

    @property
    def CommandInProgress(self):
        return self.command

    @CommandInProgress.setter
    def CommandInProgress(self, value):
        self.command = value
        if value:
            raise RuntimeError("native setter failed after entering command mode")


def test_failed_command_setup_restores_application_mode_before_returning(cad):
    session, model, _ = cad
    app = CommandStartFailure(model)
    session._sw = app
    called = []

    with pytest.raises(Exception):
        session.run_guarded(lambda: called.append(True))
    assert called == []
    assert app.CommandInProgress is False
    assert model.Extension.start_count == 0


def test_native_interface_binding_failure_happens_before_any_user_document_mutation(cad, monkeypatch):
    session, model, app = cad
    selected = [(object(), 4)]
    model.SelectionManager.items[:] = selected
    baseline = model.capture()
    called = []
    original = binding.wrap

    def failed_binding(value, interface):
        if interface == "IModelDoc2":
            raise CadError("NATIVE_BINDING_UNAVAILABLE", "Installed native interface metadata is missing.")
        return original(value, interface)

    monkeypatch.setattr(binding, "wrap", failed_binding)
    with pytest.raises(CadError, match="NATIVE_BINDING_UNAVAILABLE"):
        session.run_guarded(lambda: called.append(True))
    assert called == []
    assert model.capture() == baseline
    assert model.SelectionManager.items == selected
    assert model.Extension.start_count == 0
    assert app.CommandInProgress is False


def test_observing_an_existing_document_does_not_authorize_mutation():
    model = Model()
    session = NativeSession(App(model))
    assert session._require_model() is model
    called = []
    with pytest.raises(CadError, match="DOCUMENT_NOT_AUTHORIZED"):
        session.run_guarded(lambda: called.append(True))
    assert called == []
    assert model.Extension.start_count == 0


@pytest.mark.parametrize("state, code", [("read_only", "DOCUMENT_READ_ONLY"),
                                         ("view_only", "DOCUMENT_READ_ONLY"),
                                         ("active_sketch", "USER_SKETCH_ACTIVE"),
                                         ("editing_self", "IN_CONTEXT_EDIT_ACTIVE")])
def test_incompatible_document_state_refuses_handler_before_undo(cad, state, code):
    session, model, app = cad
    setattr(model, state, False if state == "editing_self" else object() if state == "active_sketch" else True)
    called = []
    with pytest.raises(CadError) as caught:
        session.run_guarded(lambda: called.append(True))
    assert caught.value.code == code
    assert called == []
    assert model.Extension.start_count == 0
    assert app.CommandInProgress is False


def test_switching_active_document_cannot_silently_redirect_mutation(cad):
    session, model, app = cad
    foreign = Model()
    app.ActiveDoc = foreign
    session.remember(foreign, editable=True)
    called = []
    with pytest.raises(CadError, match="ACTIVE_DOCUMENT_CHANGED"):
        session.run_guarded(lambda: called.append(True))
    assert called == []
    assert model.Extension.start_count == foreign.Extension.start_count == 0


def test_active_target_change_during_operation_is_not_a_success(cad):
    session, model, app = cad
    foreign = Model()
    foreign_selected = [(object(), 4)]
    foreign.SelectionManager.items[:] = foreign_selected

    def interrupted():
        app.ActiveDoc = foreign
        return {"ok": True}

    with pytest.raises(CadError):
        session.run_guarded(interrupted)
    assert foreign.SelectionManager.items == foreign_selected
    assert foreign.Extension.start_count == 0
    assert app.CommandInProgress is False


def test_session_and_handler_are_never_used_from_another_thread(cad):
    session, model, app = cad
    called = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(session.run_guarded, lambda: called.append(True))
        with pytest.raises(CadError, match="WRONG_COM_THREAD"):
            result.result(timeout=2)
    assert called == []
    assert model.Extension.start_count == 0
    assert app.CommandInProgress is False


def test_read_only_inspection_preserves_selection_and_never_records_undo(cad):
    session, model, app = cad
    model.read_only = True
    selected = [(object(), 4)]
    model.SelectionManager.items[:] = selected

    def get_mass_properties():
        assert app.CommandInProgress is False
        assert model.ActiveView.EnableGraphicsUpdate is True
        return {"ok": True, "volume_mm3": model.volume_mm3}

    assert session.run_guarded(get_mass_properties)["volume_mm3"] == 1250.
    assert model.Extension.start_count == model.Extension.finish_count == model.undo_count == 0
    assert model.SelectionManager.items == selected
    assert model.clear_count == 0


def test_document_identity_uses_native_object_identity_between_wrappers():
    identity = object()
    first = SimpleNamespace(_oleobj_=identity)
    second = SimpleNamespace(_oleobj_=identity)
    assert same_object(first, second)
    assert not same_object(first, SimpleNamespace(_oleobj_=object()))
    assert not same_object(first, None)
    assert same_object(None, None)


class NativeIdentityFailure(RuntimeError):
    def __init__(self, hresult):
        super().__init__("native identity comparison failed")
        self.hresult = hresult


class DocumentIdentity:
    """A close-sensitive IUnknown identity without loading the COM runtime."""
    def __init__(self, token=None, *, failure=None):
        self.token = object() if token is None else token
        self.failure = failure
        self.closed = False
        self.comparisons_after_close = 0

    def __eq__(self, other):
        if self.closed:
            self.comparisons_after_close += 1
            raise NativeIdentityFailure(-2147417848)
        if self.failure is not None:
            raise self.failure
        if isinstance(other, DocumentIdentity):
            if other.closed or other.failure is not None:
                return other == self
            return self.token is other.token
        return False


@pytest.mark.parametrize("hresult", [-2147417848, -2147220995, -2147023174])
@pytest.mark.parametrize("stale_first", [True, False])
def test_disconnected_identity_never_matches_a_live_document(hresult, stale_first):
    token = object()
    live = SimpleNamespace(_oleobj_=DocumentIdentity(token))
    stale = SimpleNamespace(_oleobj_=DocumentIdentity(token, failure=NativeIdentityFailure(hresult)))
    first, second = (stale, live) if stale_first else (live, stale)
    assert same_object(first, second) is False


@pytest.mark.parametrize("hresult", [-2147418111, -2147417846, -2147467259, None])
def test_busy_or_unknown_identity_errors_are_not_hidden(hresult):
    failure = NativeIdentityFailure(hresult)
    first = SimpleNamespace(_oleobj_=DocumentIdentity(failure=failure))
    with pytest.raises(NativeIdentityFailure) as error:
        same_object(first, SimpleNamespace(_oleobj_=DocumentIdentity()))
    assert error.value is failure


def test_native_close_forgets_ownership_before_its_handle_disconnects(cad, monkeypatch):
    from solidworks_mcp_pro.native.documents import close_part

    session, model, app = cad
    identity = DocumentIdentity()
    model._oleobj_ = identity
    other = SimpleNamespace(_oleobj_=DocumentIdentity())
    session.remember(other, editable=True, opened=True)
    monkeypatch.setattr(binding, "wrap", lambda value, interface: value)
    closed = []
    title = model.GetTitle()

    def closed_document_access():
        pytest.fail("The native close invalidated this document handle")

    def native_close(native_title):
        assert session.rights(model) == {"editable": False, "opened": False}
        assert session.rights(other)["opened"] is True
        closed.append(native_title)
        identity.closed = True
        model.GetTitle = closed_document_access
        model.GetPathName = closed_document_access
        app.ActiveDoc = None

    app.CloseDoc = native_close
    app.GetDocuments = lambda: []
    result = close_part(session)
    assert result["ok"] and closed == [title]
    assert session._model is None
    assert identity.comparisons_after_close == 0
    assert session.rights(other)["editable"] is True
    assert session._documents_rights == [{"model": other, "editable": True, "opened": True}]


def test_forget_matches_live_native_identity_without_changing_foreign_rights():
    session = NativeSession()
    identity = object()
    owned = SimpleNamespace(_oleobj_=DocumentIdentity(identity), title="Shared title")
    alias = SimpleNamespace(_oleobj_=DocumentIdentity(identity), title="Different wrapper")
    foreign = SimpleNamespace(_oleobj_=DocumentIdentity(), title="Shared title")
    session.remember(owned, editable=True, opened=True)
    session.remember(foreign)
    foreign_record = session.rights(foreign)
    session.forget(alias)
    assert session.rights(owned) == {"editable": False, "opened": False}
    assert session.rights(foreign) is foreign_record
    assert not foreign_record["editable"] and not foreign_record["opened"]
    assert session._documents_rights == [foreign_record]


def test_stale_owned_record_never_grants_rights_to_a_foreign_document():
    session = NativeSession()
    stale = SimpleNamespace(_oleobj_=DocumentIdentity())
    preserved = SimpleNamespace(_oleobj_=DocumentIdentity())
    foreign = SimpleNamespace(_oleobj_=DocumentIdentity())
    session.remember(stale, editable=True, opened=True)
    session.remember(preserved, editable=True, opened=True)
    stale._oleobj_.closed = True
    assert session.rights(foreign) == {"editable": False, "opened": False}
    session.remember(foreign)
    assert not session.rights(foreign)["editable"] and not session.rights(foreign)["opened"]
    assert session.rights(preserved)["editable"] and session.rights(preserved)["opened"]
    session.forget(foreign)
    assert session.rights(preserved)["editable"] and session.rights(preserved)["opened"]


def test_remembering_same_owned_document_preserves_granted_rights(cad):
    session, model, _ = cad
    session.remember(model)
    session.remember(model, editable=False, opened=False)
    assert session.rights(model)["editable"] is True
    assert session.rights(model)["opened"] is True
    assert len(session._documents_rights) == 1


def test_dispose_releases_session_references_and_rights_without_closing_user_documents(cad):
    session, model, app = cad
    session.dispose()
    assert session._sw is None and session._model is None
    assert session._documents_rights == []
    assert session._topology_cache == {}
    assert app.ActiveDoc is model
    assert model.Extension.start_count == 0
