"""Document ownership, file evidence and restoration against offline native doubles."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.native import binding, documents as d, session as native_session
from solidworks_mcp_pro.native.errors import CadError
from solidworks_mcp_pro.native.session import NativeSession


NATIVE_ENUMS = {'swSTLQuality_Coarse': 1, 'swSTLQuality_Fine': 2,
                'swSTLQuality_Custom': 3, 'swMM': 0}


class Document:
    def __init__(self, app, title, kind=1, path=''):
        self.app, self.title, self.kind, self.path = app, title, kind, str(path)
        self.closed, self.dirty, self.Visible = False, True, False
        self.identity = object()
        self.save_result = 0
        self.save_calls = []
        self.views_ok = True

    @property
    def _oleobj_(self):
        if self.closed:
            raise RuntimeError('The native proxy is disconnected after CloseDoc.')
        return self.identity

    def _live(self):
        if self.closed:
            raise RuntimeError('A closed document was accessed.')

    def GetTitle(self):
        self._live()
        return self.title

    def GetPathName(self):
        self._live()
        return self.path

    def GetType(self):
        self._live()
        return self.kind

    def GetSaveFlag(self):
        self._live()
        return self.dirty

    def SaveAs3(self, path, version, options):
        self._live()
        self.save_calls.append((path, version, options))
        if type(self.save_result) is int and self.save_result == 0 or self.save_result is True:
            Path(path).write_bytes(b'offline native-file fixture')
            if Path(path).suffix.lower() in {'.sldprt', '.sldasm', '.slddrw'}:
                self.path, self.dirty = path, False
        return self.save_result

    def Create1stAngleViews2(self, path):
        return self.views_ok

    def CreateDrawViewFromModelView3(self, path, view, x, y, z):
        return object()

    def InsertModelAnnotations3(self, *args):
        return [object(), object()]


class Application:
    def __init__(self):
        self.ActiveDoc = None
        self.Visible = False
        self.CommandInProgress = False
        self.documents, self.events, self.templates = [], [], {}
        self.preferences = {'swSTLQuality': 1, 'swSTLDeviation': .001, 'swSTLAngleTolerance': .5}
        self.preference_writes = []
        self.activate_result = None
        self.open_result = None
        self.close_hidden_documents = False

    def GetDocuments(self):
        return list(self.documents)

    def GetUserPreferenceStringValue(self, key):
        return self.templates.get(key, '')

    def NewDocument(self, template, paper, width, height):
        assert self.CommandInProgress is True, 'Native document creation must run inside its command scope.'
        kind = {'.prtdot': 1, '.asmdot': 2, '.drwdot': 3}[Path(template).suffix]
        model = Document(self, f'Created{len(self.documents)+1}', kind)
        self.documents.append(model)
        self.ActiveDoc = model
        self.events.append(('new', template, paper, width, height))
        return model

    def ActivateDoc3(self, title, use_preferences, rebuild, error):
        self.events.append(('activate', title))
        model = (self.activate_result if self.activate_result is not None else
                 next((v for v in self.documents if v.title == title), None))
        self.ActiveDoc = model
        return model, 0

    def CloseDoc(self, title):
        model = next(v for v in self.documents if v.title == title)
        self.events.append(('close', title))
        self.documents.remove(model)
        model.closed = True
        self.ActiveDoc = self.documents[-1] if self.documents else None
        if self.close_hidden_documents:
            hidden = [value for value in self.documents if not value.Visible]
            for value in hidden:
                self.documents.remove(value)
                value.closed = True
            self.ActiveDoc = self.documents[-1] if self.documents else None

    def OpenDoc6(self, path, kind, options, configuration, errors, warnings):
        self.events.append(('open', path, kind, options))
        if self.open_result is not None:
            return self.open_result
        model = Document(self, Path(path).name, kind, path)
        self.documents.append(model)
        self.ActiveDoc = model
        return model, 0, 0

    def _get_preference(self, key):
        return self.preferences.get(key, 0)

    def _set_preference(self, key, value):
        self.preference_writes.append((key, value))
        self.preferences[key] = value
        return True

    GetUserPreferenceToggle = _get_preference
    GetUserPreferenceIntegerValue = _get_preference
    GetUserPreferenceDoubleValue = _get_preference
    SetUserPreferenceToggle = _set_preference
    SetUserPreferenceIntegerValue = _set_preference
    SetUserPreferenceDoubleValue = _set_preference


class CoupledFrame:
    def __init__(self, app):
        self.app = app
        self._keep_invisible = False
        self.writes = []
        self.show_when_released = False

    @property
    def KeepInvisible(self):
        return self._keep_invisible

    @KeepInvisible.setter
    def KeepInvisible(self, value):
        self.writes.append(bool(value))
        self._keep_invisible = bool(value)
        if not value and self.show_when_released:
            self.app.foreground_transitions.append('released frame')
            self.app._visible = True


class CoupledApplication(Application):
    """Enforce the native difference between visibility and user control."""
    def __init__(self):
        self._visible, self._user_control = False, True
        self.visible_writes, self.background_writes, self.foreground_transitions = [], [], []
        self.background_hiding_allowed = True
        self.visible_hiding_allowed = True
        self.frame = CoupledFrame(self)
        super().__init__()
        self.visible_writes.clear()
        self.close_hidden_documents = True

    def Frame(self):
        return self.frame

    @property
    def Visible(self):
        return self._visible

    @Visible.setter
    def Visible(self, value):
        self.visible_writes.append(bool(value))
        # The native Visible=False setter cannot hide a user-controlled app.
        if value or not self._user_control and self.visible_hiding_allowed:
            self._visible = bool(value)

    @property
    def UserControl(self):
        return self._user_control

    @UserControl.setter
    def UserControl(self, value):
        self._user_control = bool(value)
        if value:
            self._visible = True

    @property
    def UserControlBackground(self):
        return self._user_control

    @UserControlBackground.setter
    def UserControlBackground(self, value):
        self.background_writes.append(bool(value))
        self._user_control = bool(value)
        if value and self.background_hiding_allowed:
            self._visible = False

    def document_became_visible(self, document):
        if not self.frame.KeepInvisible:
            self.foreground_transitions.append(document.title)
            self._visible = True


class CoupledDocument(Document):
    @property
    def Visible(self):
        return self._document_visible

    @Visible.setter
    def Visible(self, value):
        self._document_visible = bool(value)
        if value:
            self.app.document_became_visible(self)


class DelayedBackgroundApplication(CoupledApplication):
    def __init__(self, clock):
        self.clock, self.background_due = clock, None
        super().__init__()

    @property
    def Visible(self):
        if self.background_due is not None and self.clock.now >= self.background_due:
            self._visible = False
        return self._visible

    @Visible.setter
    def Visible(self, value):
        CoupledApplication.Visible.fset(self, value)

    @property
    def UserControlBackground(self):
        return self._user_control

    @UserControlBackground.setter
    def UserControlBackground(self, value):
        self.background_writes.append(bool(value))
        self._user_control = bool(value)
        if value:
            self.background_due = self.clock.now + .06


class BackgroundModeWithoutHidingApplication(CoupledApplication):
    def __init__(self):
        self.background_selected = False
        super().__init__()

    @property
    def Visible(self):
        return self._visible

    @Visible.setter
    def Visible(self, value):
        self.visible_writes.append(bool(value))
        if value or not self._user_control or self.background_selected:
            self._visible = bool(value)

    @property
    def UserControlBackground(self):
        return self._user_control

    @UserControlBackground.setter
    def UserControlBackground(self, value):
        self.background_writes.append(bool(value))
        self._user_control = bool(value)
        self.background_selected = bool(value)


class StrictControlVisibilityApplication(CoupledApplication):
    """A selected control mode is not enough: only released control permits hiding."""
    def __init__(self):
        self.control_events = []
        self.reject_visibility_while_uncontrolled = False
        self.reject_control_restoration = False
        super().__init__()
        self.background_hiding_allowed = False
        self.control_events.clear()

    @property
    def Visible(self):
        return self._visible

    @Visible.setter
    def Visible(self, value):
        self.control_events.append(('visible', bool(value), self._user_control))
        if not value and not self._user_control and self.reject_visibility_while_uncontrolled:
            raise RuntimeError('native visibility setter failed')
        CoupledApplication.Visible.fset(self, value)

    @property
    def UserControlBackground(self):
        return self._user_control

    @UserControlBackground.setter
    def UserControlBackground(self, value):
        self.control_events.append(('background', bool(value), self._user_control))
        if value and not self._user_control and self.reject_control_restoration:
            self.background_writes.append(True)
            raise RuntimeError('native control restoration failed')
        CoupledApplication.UserControlBackground.fset(self, value)


@pytest.fixture
def cad(tmp_path, monkeypatch):
    monkeypatch.setattr(binding, 'wrap', lambda value, interface: value)
    monkeypatch.setattr(binding, 'constant', lambda name: NATIVE_ENUMS.get(name, name))
    app = Application()
    source = tmp_path / 'source.sldprt'
    source.write_bytes(b'saved part fixture')
    model = Document(app, 'Source', path=source)
    app.documents.append(model)
    app.ActiveDoc = model
    session = NativeSession(app)
    session._model = model
    session.remember(model, editable=True, opened=True)
    original_forget = session.forget
    def forget(document):
        app.events.append(('forget', document.GetTitle()))
        original_forget(document)
    session.forget = forget
    return session, app, model


@pytest.fixture
def coupled_cad(cad):
    # Reuse only the SDK-isolation fixture; this is a fresh real NativeSession.
    app = CoupledApplication()
    target = CoupledDocument(app, 'Target')
    foreign = CoupledDocument(app, 'HiddenForeign')
    app.documents = [target, foreign]
    app.ActiveDoc = target
    session = NativeSession(app)
    session._model = target
    session.remember(target, editable=True, opened=True)
    return session, app, target, foreign


@pytest.fixture
def ui_clock(monkeypatch):
    clock = SimpleNamespace(now=0., sleeps=[])
    def sleep(duration):
        clock.sleeps.append(duration)
        clock.now += duration
    monkeypatch.setattr(native_session, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return clock


def background_session(app):
    target = CoupledDocument(app, 'Target')
    foreign = CoupledDocument(app, 'HiddenForeign')
    app.documents = [target, foreign]
    app.ActiveDoc = target
    app.Frame = None
    session = NativeSession(app)
    session._model = target
    session.remember(target, editable=True, opened=True)
    return session, target, foreign


def install_template(app, tmp_path, kind):
    key, suffix = {1: ('swDefaultTemplatePart', '.prtdot'),
                   2: ('swDefaultTemplateAssembly', '.asmdot'),
                   3: ('swDefaultTemplateDrawing', '.drwdot')}[kind]
    path = tmp_path / ('default'+suffix)
    path.write_bytes(b'native template fixture')
    app.templates[key] = str(path)


def couple_stl_preferences(app, *, quality=3, units=2):
    """Native presets replace both tolerances; manual tolerances select Custom."""
    presets = {1: (.004, .6), 2: (.00012, .08)}
    deviation, angle = presets.get(quality, (.000371, .173))
    app.preferences.update(swSTLBinaryFormat=False, swSTLDontTranslateToPositive=False,
        swExportStlUnits=units, swSTLComponentsIntoOneFile=False, swSTLCheckForInterference=True,
        swSTLPreview=True, swSTLShowInfoOnSave=False, swSTLDeviation=deviation,
        swSTLAngleTolerance=angle, swSTLQuality=quality)
    events = []
    read, write = app._get_preference, app._set_preference
    def getter(key):
        events.append(('read', key))
        return read(key)
    def setter(key, value):
        events.append(('write', key, value))
        result = write(key, value)
        if key == 'swExportStlUnits':
            # Unit changes can also select another native quality preset.
            app.preferences.update(swSTLQuality=1, swSTLDeviation=presets[1][0],
                                   swSTLAngleTolerance=presets[1][1])
        elif key == 'swSTLQuality' and value in presets:
            app.preferences['swSTLDeviation'], app.preferences['swSTLAngleTolerance'] = presets[value]
        elif key in {'swSTLDeviation', 'swSTLAngleTolerance'}:
            app.preferences['swSTLQuality'] = 3
        return result
    app.GetUserPreferenceToggle = getter
    app.GetUserPreferenceIntegerValue = getter
    app.GetUserPreferenceDoubleValue = getter
    app.SetUserPreferenceToggle = setter
    app.SetUserPreferenceIntegerValue = setter
    app.SetUserPreferenceDoubleValue = setter
    return events, presets


@pytest.mark.parametrize('path', ['', None, '\x00', '\\\\server\\part.sldprt'])
def test_invalid_or_network_file_path_is_rejected_without_creation(path):
    with pytest.raises(CadError) as caught:
        d._file(path)
    assert caught.value.code == 'INVALID_PATH'


def test_output_path_is_absolute_with_default_suffix_and_created_parent(tmp_path):
    target = d._file(str(tmp_path / 'nested' / 'part'), '.sldprt')
    assert target.is_absolute() and target.suffix == '.sldprt'
    assert target.parent.is_dir() and not target.exists()


def test_evidence_rejects_missing_and_empty_output(tmp_path):
    path = tmp_path / 'empty.step'
    with pytest.raises(CadError, match='not written'):
        d._evidence(path)
    path.touch()
    with pytest.raises(CadError, match='not written'):
        d._evidence(path)


def test_default_template_is_required_before_native_creation(cad):
    session, app, original = cad
    with pytest.raises(CadError) as caught:
        d.new_part(session)
    assert caught.value.code == 'DEFAULT_TEMPLATE_MISSING'
    assert not app.events and session._model is original


@pytest.mark.parametrize('kind,function', [(1, d.new_part), (2, d.new_assembly)])
def test_new_document_uses_configured_native_template_and_grants_ownership(cad, tmp_path, kind, function):
    session, app, original = cad
    install_template(app, tmp_path, kind)
    result = function(session)
    assert result['type'] == kind and result['saved'] is False
    assert session._model is app.ActiveDoc and session._model is not original
    assert session.rights(app.ActiveDoc)['opened'] and session.rights(app.ActiveDoc)['editable']


@pytest.mark.parametrize('before', [False, True])
@pytest.mark.parametrize('kind,function', [(1, d.new_part), (2, d.new_assembly)])
def test_new_document_batches_factory_and_ownership_then_restores_command_state(cad, tmp_path, before, kind, function):
    session, app, original = cad
    app.CommandInProgress = before
    install_template(app, tmp_path, kind)
    remember = session.remember
    def verify_ownership(*args, **kwargs):
        assert app.CommandInProgress is True
        remember(*args, **kwargs)
    session.remember = verify_ownership
    result = function(session)
    assert result['created'] and app.CommandInProgress is before


@pytest.mark.parametrize('before', [False, True])
@pytest.mark.parametrize('failure', ['native_error', 'missing_document', 'wrong_active_document'])
def test_new_document_restores_command_state_when_creation_or_verification_fails(cad, tmp_path, before, failure):
    session, app, original = cad
    app.CommandInProgress = before
    install_template(app, tmp_path, 1)
    create = app.NewDocument
    def fail(*args):
        assert app.CommandInProgress is True
        if failure == 'native_error':
            raise RuntimeError('native factory failed')
        if failure == 'missing_document':
            return None
        created = create(*args)
        app.ActiveDoc = original
        return created
    app.NewDocument = fail
    with pytest.raises(RuntimeError):
        d.new_part(session)
    assert app.CommandInProgress is before
    assert session._model is original


def test_activation_requires_unique_exact_title_and_explicit_edit_adoption(cad):
    session, app, model = cad
    foreign = Document(app, 'Foreign')
    app.documents.append(foreign)
    result = d.activate_document(session, 'Foreign')
    assert result['document']['editable'] is False
    assert session._model is foreign
    d.activate_document(session, 'Foreign', allow_edit=True)
    assert session.rights(foreign)['editable'] is True
    app.documents.append(Document(app, 'Foreign'))
    with pytest.raises(CadError) as caught:
        d.activate_document(session, 'Foreign')
    assert caught.value.code == 'DOCUMENT_NOT_UNIQUE'


def test_native_open_defaults_to_observation_and_retains_file_identity(cad, tmp_path):
    session, app, original = cad
    source = tmp_path / 'opened.sldprt'
    source.write_bytes(b'part fixture')
    result = d.open_part(session, str(source))
    assert result['source_path'] == str(source.resolve())
    assert result['document']['opened_by_session'] is True
    assert result['document']['editable'] is False
    assert session._model is app.ActiveDoc and session._model is not original


def test_wrong_native_file_type_is_rejected_before_open(cad, tmp_path):
    session, app, original = cad
    path = tmp_path / 'assembly.sldasm'
    path.write_bytes(b'assembly fixture')
    with pytest.raises(CadError) as caught:
        d.open_part(session, str(path))
    assert caught.value.code == 'DOCUMENT_TYPE_MISMATCH'
    assert not app.events and session._model is original


def test_large_file_opt_in_is_checked_before_calling_native_open(cad, tmp_path):
    session, app, original = cad
    path = tmp_path / 'large.sldprt'
    with path.open('wb') as stream:
        stream.truncate(30*1024*1024+1)
    with pytest.raises(CadError) as caught:
        d.open_part(session, str(path))
    assert caught.value.code == 'LARGE_FILE_OPT_IN_REQUIRED'
    assert not app.events


@pytest.mark.parametrize('returned', [False, 2, None])
def test_save_failure_cannot_be_hidden_by_a_preexisting_file(cad, tmp_path, returned):
    session, app, model = cad
    target = tmp_path / 'existing.sldprt'
    target.write_bytes(b'previous bytes')
    model.save_result = returned
    with pytest.raises(CadError) as caught:
        d.save_part(session, str(target))
    assert caught.value.code in {'NATIVE_SAVE_FAILED', 'NATIVE_SAVE_RESULT_INVALID'}
    assert target.read_bytes() == b'previous bytes'


def test_native_save_proves_file_hash_identity_and_clean_state(cad, tmp_path):
    session, app, model = cad
    result = d.save_part(session, str(tmp_path / 'saved'))
    assert result['saved'] and result['bytes'] > 0 and len(result['sha256']) == 64
    assert Path(model.path).resolve() == Path(result['path'])
    assert not model.dirty


def test_save_part_never_invokes_export_for_an_incompatible_extension(cad, tmp_path):
    session, app, model = cad
    with pytest.raises(CadError) as caught:
        d.save_part(session, str(tmp_path / 'part.step'))
    assert caught.value.code == 'DOCUMENT_TYPE_MISMATCH'
    assert not model.save_calls


def test_close_forgets_ownership_before_native_proxy_is_invalidated(cad):
    session, app, model = cad
    result = d.close_part(session)
    assert result['closed'] == 'Source' and result['saved'] is False
    assert app.events == [('forget', 'Source'), ('close', 'Source')]
    assert model.closed and session._model is None and not session._documents_rights


def test_close_rejects_a_document_not_opened_or_adopted_by_the_session(cad):
    session, app, model = cad
    session._documents_rights.clear()
    with pytest.raises(CadError) as caught:
        d.close_part(session)
    assert caught.value.code == 'DOCUMENT_NOT_AUTHORIZED'
    assert not model.closed and not app.events


def test_unsaved_close_with_save_needs_an_explicit_path(cad):
    session, app, model = cad
    model.path = ''
    with pytest.raises(CadError) as caught:
        d.close_part(session, save=True)
    assert caught.value.code == 'SAVE_PATH_REQUIRED'
    assert not model.closed and not model.save_calls


def test_export_restores_global_tessellation_settings_when_save_fails(cad, tmp_path):
    session, app, model = cad
    app.preferences.update(swSTLBinaryFormat=False, swSTLQuality=1, swSTLDeviation=.2)
    before = dict(app.preferences)
    model.save_result = 1
    with pytest.raises(CadError) as caught:
        d.export(session, str(tmp_path / 'mesh.stl'), deviation_mm=.02, angle_deg=15)
    assert caught.value.code == 'NATIVE_SAVE_FAILED'
    assert all(app.preferences[key] == value for key, value in before.items())
    assert app.preferences['swSTLAngleTolerance'] == .5
    assert model.path.endswith('source.sldprt')


def test_export_reports_unrestorable_native_settings_even_if_file_was_written(cad, tmp_path):
    session, app, model = cad
    original_set = app.SetUserPreferenceToggle
    def setter(key, value):
        if key == 'swSTLBinaryFormat' and value == 0 and app.preferences.get(key) is True:
            return False
        return original_set(key, value)
    app.SetUserPreferenceToggle = setter
    with pytest.raises(CadError) as caught:
        d.export(session, str(tmp_path / 'mesh.stl'))
    assert caught.value.code == 'EXPORT_SETTINGS_RESTORE_FAILED'
    assert caught.value.details['outcome_may_be_indeterminate'] is True


@pytest.mark.parametrize('args', [{'file_format': 'step'}, {'file_format': 'unrecognized'},
                                 {'file_format': 'iges', 'per_component': True}])
def test_export_format_conflicts_are_rejected_before_native_save(cad, tmp_path, args):
    session, app, model = cad
    with pytest.raises(CadError):
        d.export(session, str(tmp_path / 'mesh.stl'), **args)
    assert not model.save_calls and not app.preference_writes


def test_per_component_step_is_rejected_before_native_export(cad, tmp_path):
    session, app, model = cad
    with pytest.raises(CadError) as caught:
        d.export(session, str(tmp_path / 'assembly.step'), per_component=True)
    assert caught.value.code == 'COMPONENT_EXPORT_FORMAT_UNSUPPORTED'
    assert not model.save_calls


def test_custom_angle_alone_activates_native_custom_tessellation(cad, tmp_path):
    session, app, model = cad
    result = d.export(session, str(tmp_path / 'mesh.stl'), angle_deg=15)
    assert result['source_path_unchanged'] is True
    assert ('swSTLQuality', 3) in app.preference_writes
    assert app.preferences['swSTLQuality'] == 1


def test_stl_snapshots_all_original_preferences_before_any_coupled_write(cad, tmp_path):
    session, app, model = cad
    events, presets = couple_stl_preferences(app)
    original = dict(app.preferences)
    save = model.SaveAs3
    def inspect_export(path, *args):
        assert app.preferences['swSTLQuality'] == 2
        assert (app.preferences['swSTLDeviation'], app.preferences['swSTLAngleTolerance']) == presets[2]
        return save(path, *args)
    model.SaveAs3 = inspect_export
    d.export(session, str(tmp_path / 'fine.stl'))
    first_write = next(index for index, event in enumerate(events) if event[0] == 'write')
    assert {event[1] for event in events[:first_write]} == set(original)
    assert app.preferences == original


def test_stl_custom_export_restores_original_tolerances_then_preset_quality(cad, tmp_path):
    session, app, model = cad
    events, presets = couple_stl_preferences(app, quality=2)
    original = dict(app.preferences)
    d.export(session, str(tmp_path / 'custom.stl'), deviation_mm=.02, angle_deg=15)
    assert app.preferences == original
    writes = [event for event in events if event[0] == 'write']
    assert writes[-1] == ('write', 'swSTLQuality', 2)


def test_stl_export_restores_coupled_custom_state_even_when_native_save_fails(cad, tmp_path):
    session, app, model = cad
    couple_stl_preferences(app)
    original = dict(app.preferences)
    model.save_result = 2
    with pytest.raises(CadError) as caught:
        d.export(session, str(tmp_path / 'failed.stl'), quality='coarse')
    assert caught.value.code == 'NATIVE_SAVE_FAILED'
    assert app.preferences == original


def test_custom_angle_preserves_unspecified_deviation_despite_quality_side_effects(cad, tmp_path):
    session, app, model = cad
    couple_stl_preferences(app, quality=2)
    original = dict(app.preferences)
    save = model.SaveAs3
    def inspect_export(path, *args):
        assert app.preferences['swSTLQuality'] == 3
        assert app.preferences['swSTLDeviation'] == original['swSTLDeviation']
        assert app.preferences['swSTLAngleTolerance'] == pytest.approx(.2617993877991494)
        return save(path, *args)
    model.SaveAs3 = inspect_export
    d.export(session, str(tmp_path / 'angle.stl'), angle_deg=15)
    assert app.preferences == original


def test_stl_snapshot_failure_causes_no_preference_write_or_native_export(cad, tmp_path):
    session, app, model = cad
    read = app.GetUserPreferenceDoubleValue
    def fail(key):
        if key == 'swSTLAngleTolerance':
            raise RuntimeError('native tolerance unavailable')
        return read(key)
    app.GetUserPreferenceDoubleValue = fail
    with pytest.raises(RuntimeError, match='native tolerance unavailable'):
        d.export(session, str(tmp_path / 'mesh.stl'))
    assert not app.preference_writes and not model.save_calls


def test_stl_final_verification_catches_a_later_setter_corrupting_prior_tolerance(cad, tmp_path):
    session, app, model = cad
    couple_stl_preferences(app, quality=2)
    setter = app.SetUserPreferenceIntegerValue
    def corrupt(key, value):
        result = setter(key, value)
        if key == 'swSTLQuality' and value == 2:
            app.preferences['swSTLAngleTolerance'] = .99
        return result
    app.SetUserPreferenceIntegerValue = corrupt
    with pytest.raises(CadError) as caught:
        d.export(session, str(tmp_path / 'mesh.stl'), deviation_mm=.02, angle_deg=15)
    assert caught.value.code == 'EXPORT_SETTINGS_RESTORE_FAILED'
    assert 'swSTLAngleTolerance' in caught.value.message
    assert caught.value.details['outcome_may_be_indeterminate'] is True


def test_native_export_cannot_report_success_if_it_changed_source_identity(cad, tmp_path):
    session, app, model = cad
    save = model.SaveAs3
    def rename(path, *args):
        result = save(path, *args)
        model.path = path
        return result
    model.SaveAs3 = rename
    with pytest.raises(CadError) as caught:
        d.export(session, str(tmp_path / 'mesh.step'))
    assert caught.value.code == 'EXPORT_SOURCE_IDENTITY_CHANGED'
    assert caught.value.details['outcome_may_be_indeterminate'] is True


def test_drawing_failure_closes_only_its_created_document_then_restores_source(cad, tmp_path):
    session, app, source = cad
    install_template(app, tmp_path, 3)
    create = app.NewDocument
    def new(*args):
        drawing = create(*args)
        drawing.views_ok = False
        return drawing
    app.NewDocument = new
    with pytest.raises(CadError) as caught:
        d.make_drawing(session, str(tmp_path / 'drawing.pdf'))
    assert caught.value.code == 'DRAWING_VIEWS_FAILED'
    assert app.ActiveDoc is source and session._model is source
    assert app.documents == [source] and session.rights(source)['editable']
    assert app.events[-3:] == [('forget', 'Created2'), ('close', 'Created2'), ('activate', 'Source')]


def test_drawing_restoration_failure_exposes_safe_actual_document_identity(cad, tmp_path):
    session, app, source = cad
    install_template(app, tmp_path, 3)
    foreign = Document(app, 'WrongSource', path=tmp_path / 'other.sldprt')
    app.activate_result = foreign
    with pytest.raises(CadError) as caught:
        d.make_drawing(session, str(tmp_path / 'drawing.pdf'))
    result = caught.value.result()
    assert result['code'] == 'DOCUMENT_RESTORATION_FAILED'
    assert result['checks']['returned_matches_source'] is False
    assert result['identity']['returned']['title'] == 'WrongSource'
    assert result['identity']['source']['title'] == 'Source'
    assert result['identity']['requested_path'] == source.path


def test_disconnected_document_diagnostics_do_not_mask_the_error():
    closed = SimpleNamespace(GetTitle=lambda: (_ for _ in ()).throw(RuntimeError('disconnected')),
                             GetPathName=lambda: 'readable-path', GetType=lambda: 1)
    evidence = d._identity_evidence(closed)
    assert 'disconnected' in evidence['title_error'] and evidence['path'] == 'readable-path'


def test_drawing_close_preserves_every_preexisting_hidden_document(cad, tmp_path):
    session, app, source = cad
    install_template(app, tmp_path, 3)
    app.close_hidden_documents = True
    foreign = Document(app, 'HiddenForeign', path=tmp_path / 'foreign.sldprt')
    app.documents.append(foreign)
    assert source.Visible is False and foreign.Visible is False
    d.make_drawing(session, str(tmp_path / 'drawing.pdf'))
    assert app.ActiveDoc is source and session._model is source
    assert not source.closed and not foreign.closed
    assert set(app.documents) == {source, foreign}
    assert source.Visible is False and foreign.Visible is False
    assert app.Visible is False


def test_close_preserves_other_hidden_documents_and_original_app_visibility(cad, tmp_path):
    session, app, target = cad
    app.close_hidden_documents = True
    foreign = Document(app, 'HiddenForeign', path=tmp_path / 'foreign.sldprt')
    app.documents.append(foreign)
    assert app.ActiveDoc is target
    d.close_part(session)
    assert target.closed and not foreign.closed
    assert app.documents == [foreign]
    assert foreign.Visible is False and app.Visible is False


def test_close_does_not_proceed_if_a_foreign_hidden_document_cannot_be_protected(cad):
    session, app, target = cad
    class UnshowableDocument(Document):
        @property
        def Visible(self):
            return False
        @Visible.setter
        def Visible(self, value):
            pass
    foreign = UnshowableDocument(app, 'HiddenForeign')
    app.documents.append(foreign)
    app.close_hidden_documents = True
    with pytest.raises(CadError) as caught:
        d.close_part(session)
    assert caught.value.code == 'DOCUMENT_VISIBILITY_UNVERIFIED'
    assert not target.closed and not foreign.closed
    assert not any(event[0] in {'close', 'forget'} for event in app.events)
    assert session.rights(target)['editable']


def test_close_preserves_visibility_when_native_close_raises(cad):
    session, app, target = cad
    foreign = Document(app, 'HiddenForeign')
    app.documents.append(foreign)
    def fail(title):
        assert foreign.Visible is True
        raise RuntimeError('native close rejected')
    app.CloseDoc = fail
    with pytest.raises(RuntimeError, match='native close rejected'):
        d.close_part(session)
    assert foreign.Visible is False and app.Visible is False
    assert not target.closed and not foreign.closed
    assert session.rights(target)['editable'] and session.rights(target)['opened']
    assert session._model is target


def test_hidden_user_controlled_close_uses_background_control_if_frame_is_unavailable(coupled_cad):
    session, app, target, foreign = coupled_cad
    # Demonstrate the native setter constraint so an uncoupled fake cannot make
    # a redundant Visible=False call look like a correct restoration.
    app._visible = True
    app.Visible = False
    assert app.Visible is True
    app.UserControlBackground = True
    assert app.UserControl is True and app.Visible is False
    app.visible_writes.clear()
    app.background_writes.clear()
    app.Frame = None
    d.close_part(session)
    assert target.closed and not foreign.closed and foreign.Visible is False
    assert app.Visible is False and app.UserControl is True
    assert app.background_writes == [True] and not app.visible_writes


def test_frame_protection_prevents_temporary_foreground_transition(coupled_cad):
    session, app, target, foreign = coupled_cad
    d.close_part(session)
    assert target.closed and not foreign.closed and foreign.Visible is False
    assert app.frame.writes == [True, False]
    assert app.frame.KeepInvisible is False
    assert app.foreground_transitions == []
    assert not app.background_writes and not app.visible_writes
    assert app.Visible is False and app.UserControl is True


def test_releasing_frame_restores_hidden_mode_through_background_control(coupled_cad):
    session, app, target, foreign = coupled_cad
    app.frame.show_when_released = True
    d.close_part(session)
    assert app.foreground_transitions == ['released frame']
    assert app.background_writes == [True]
    assert app.Visible is False and app.UserControl is True
    assert app.frame.KeepInvisible is False and foreign.Visible is False


def test_existing_invisible_frame_flag_is_preserved_without_redundant_setters(coupled_cad):
    session, app, target, foreign = coupled_cad
    app.frame._keep_invisible = True
    d.close_part(session)
    assert app.frame.KeepInvisible is True and not app.frame.writes
    assert not app.foreground_transitions and not app.background_writes


def test_failed_close_keeps_rights_and_coupled_background_state(coupled_cad):
    session, app, target, foreign = coupled_cad
    def refuse(title):
        assert app.frame.KeepInvisible and foreign.Visible
        raise RuntimeError('native refused the close')
    app.CloseDoc = refuse
    with pytest.raises(RuntimeError, match='native refused'):
        d.close_part(session)
    assert not target.closed and not foreign.closed
    assert session._model is target
    assert session.rights(target)['editable'] and session.rights(target)['opened']
    assert app.Visible is False and app.UserControl is True
    assert app.frame.KeepInvisible is False and foreign.Visible is False


def test_failed_background_restoration_is_reported_as_indeterminate(coupled_cad, ui_clock):
    session, app, target, foreign = coupled_cad
    app.Frame = None
    app.background_hiding_allowed = False
    app.visible_hiding_allowed = False
    with pytest.raises(CadError) as caught:
        d.close_part(session)
    assert caught.value.code == 'DOCUMENT_VISIBILITY_RESTORATION_FAILED'
    assert caught.value.details['outcome_may_be_indeterminate'] is True
    assert app.Visible is True and app.UserControl is True
    assert app.visible_writes == [False, False]
    assert app.background_writes == [True, False, True]
    assert ui_clock.now == pytest.approx(.25)


def test_background_restoration_waits_for_bounded_native_ui_settlement(cad, ui_clock):
    app = DelayedBackgroundApplication(ui_clock)
    session, target, foreign = background_session(app)
    d.close_part(session)
    assert target.closed and not foreign.closed
    assert app.Visible is False and app.UserControl is True
    assert app.background_writes == [True] and not app.visible_writes
    assert .06 <= ui_clock.now <= .25


def test_selected_background_mode_uses_explicit_visibility_fallback_without_dropping_control(cad, ui_clock):
    app = BackgroundModeWithoutHidingApplication()
    session, target, foreign = background_session(app)
    d.close_part(session)
    assert target.closed and not foreign.closed
    assert app.background_writes == [True] and app.visible_writes == [False]
    assert app.Visible is False and app.UserControl is True
    assert foreign.Visible is False and ui_clock.now == pytest.approx(.25)


def test_strict_native_hiding_releases_then_restores_control_without_waiting_or_closing(cad, ui_clock, monkeypatch):
    app = StrictControlVisibilityApplication()
    session, target, foreign = background_session(app)
    native_close = app.CloseDoc
    def close(title):
        assert app.UserControl is True
        return native_close(title)
    app.CloseDoc = close
    native_sleep = native_session.time.sleep
    def sleep(duration):
        assert app.UserControl is True, 'No wait is permitted while control is temporarily released.'
        native_sleep(duration)
    monkeypatch.setattr(native_session.time, 'sleep', sleep)
    d.close_part(session)
    assert target.closed and not foreign.closed
    assert app.control_events[-3:] == [('background', False, True), ('visible', False, False),
                                      ('background', True, False)]
    assert app.Visible is False and app.UserControl is True
    assert foreign.Visible is False and ui_clock.now == pytest.approx(.25)


def test_temporary_release_always_restores_control_if_visibility_setter_raises(cad, ui_clock):
    app = StrictControlVisibilityApplication()
    app.reject_visibility_while_uncontrolled = True
    session, target, foreign = background_session(app)
    with pytest.raises(CadError) as caught:
        d.close_part(session)
    assert caught.value.code == 'DOCUMENT_VISIBILITY_RESTORATION_FAILED'
    assert 'native visibility setter failed' in caught.value.message
    assert app.background_writes == [True, False, True]
    assert app.UserControl is True and not foreign.closed
    assert caught.value.details['outcome_may_be_indeterminate'] is True


def test_failed_control_restoration_is_verified_even_after_setter_exception(cad, ui_clock):
    app = StrictControlVisibilityApplication()
    app.reject_control_restoration = True
    session, target, foreign = background_session(app)
    with pytest.raises(CadError) as caught:
        d.close_part(session)
    assert caught.value.code == 'DOCUMENT_VISIBILITY_RESTORATION_FAILED'
    assert 'native control restoration failed' in caught.value.message
    assert 'user-control expected True, actual False' in caught.value.message
    assert app.Visible is False and app.UserControl is False
    assert not foreign.closed and caught.value.details['outcome_may_be_indeterminate'] is True


def test_final_document_visibility_is_checked_after_background_restoration(coupled_cad, monkeypatch):
    session, app, target, foreign = coupled_cad
    app.Frame = None
    original = CoupledApplication.UserControlBackground.fset
    def reveal_foreign(self, value):
        original(self, value)
        foreign._document_visible = True
    monkeypatch.setattr(CoupledApplication, 'UserControlBackground',
                        property(lambda self: self._user_control, reveal_foreign))
    with pytest.raises(CadError) as caught:
        d.close_part(session)
    assert caught.value.code == 'DOCUMENT_VISIBILITY_RESTORATION_FAILED'
    assert 'final document visibility' in caught.value.message


def test_final_frame_flag_is_checked_after_background_restoration(coupled_cad, monkeypatch):
    session, app, target, foreign = coupled_cad
    app.frame.show_when_released = True
    original = CoupledApplication.UserControlBackground.fset
    def restore_and_alter(self, value):
        original(self, value)
        self.frame._keep_invisible = True
    monkeypatch.setattr(CoupledApplication, 'UserControlBackground',
                        property(lambda self: self._user_control, restore_and_alter))
    with pytest.raises(CadError) as caught:
        d.close_part(session)
    assert caught.value.code == 'DOCUMENT_VISIBILITY_RESTORATION_FAILED'
    assert 'final frame visibility' in caught.value.message
