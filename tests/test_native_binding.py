"""Native SDK discovery and error-boundary tests without an application connection."""
from contextlib import nullcontext
import json
from types import SimpleNamespace
import sys

import pytest

from solidworks_mcp_pro.native import binding
from solidworks_mcp_pro.native.errors import CadError


def test_typelib_override_is_exact_and_constants_are_siblings(tmp_path, monkeypatch):
    library = tmp_path / 'installed-sldworks.tlb'
    constants = tmp_path / 'swconst.tlb'
    library.write_bytes(b'native SDK fixture')
    constants.write_bytes(b'native constant fixture')
    monkeypatch.setenv('SW_MCP_TYPELIB', str(library))
    assert binding.typelib_path() == library.resolve()
    assert binding.typelib_path('swconst') == constants.resolve()


def test_missing_override_does_not_silently_select_a_different_sdk(tmp_path, monkeypatch):
    monkeypatch.setenv('SW_MCP_TYPELIB', str(tmp_path / 'missing.tlb'))
    with pytest.raises(CadError) as caught:
        binding.typelib_path()
    assert caught.value.code == 'TYPE_LIBRARY_NOT_FOUND'


def test_registry_discovery_prefers_newest_version_then_native_64_bit(tmp_path, monkeypatch):
    root = 'TypeLib\\' + binding._LIBRARIES['sldworks']
    paths = {}
    for version in ['21.0', '22.0']:
        for platform in ['win64', 'win32']:
            path = tmp_path / f'{version}-{platform}.tlb'
            path.write_bytes(b'SDK fixture')
            paths[f'{root}\\{version}\\0\\{platform}'] = str(path)
    def open_key(parent, name):
        full = parent+'\\'+name if isinstance(parent, str) else name
        if full == root or full in paths:
            return nullcontext(full)
        raise FileNotFoundError(full)
    registry = SimpleNamespace(HKEY_CLASSES_ROOT=object(), OpenKey=open_key,
        QueryInfoKey=lambda key: (2, 0, 0), EnumKey=lambda key, index: ['21.0', '22.0'][index],
        QueryValueEx=lambda key, name: (paths[key], 1))
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    monkeypatch.setattr(binding, 'os', SimpleNamespace(name='nt', environ={'PROGRAMFILES': str(tmp_path)}))
    assert binding.typelib_path() == (tmp_path / '22.0-win64.tlb').resolve()


def test_unknown_type_library_is_rejected_before_loading():
    with pytest.raises(CadError) as caught:
        binding.typelib_path('unrelated-library')
    assert caught.value.code == 'TYPE_LIBRARY_UNKNOWN'


def test_sdk_metadata_generation_is_cached_without_an_application_connection(tmp_path, monkeypatch):
    loads, generations = [], []
    library_path = tmp_path / 'sldworks.tlb'
    metadata = object()
    native_library = SimpleNamespace(GetLibAttr=lambda: ('native-sdk-guid', 0, 3, 34, 0, 0))
    pythoncom = SimpleNamespace(LoadTypeLib=lambda path: (loads.append(path), native_library)[1])
    cache = SimpleNamespace(EnsureModule=lambda *args: (generations.append(args), metadata)[1])
    monkeypatch.setitem(sys.modules, 'pythoncom', pythoncom)
    monkeypatch.setitem(sys.modules, 'win32com.client', SimpleNamespace(gencache=cache))
    monkeypatch.setattr(binding, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(binding, '_modules', {})
    monkeypatch.setattr(binding, 'typelib_path', lambda name: library_path)
    assert binding.module() is metadata and binding.module() is metadata
    assert loads == [str(library_path)]
    assert generations == [('native-sdk-guid', 0, 34, 0)]


def test_wrapper_uses_the_requested_native_interface(monkeypatch):
    calls = []
    dispatch = object()
    class Interface:
        CLSID = 'fixture-interface-iid'
        def __init__(self, value):
            self.value = value
    native = SimpleNamespace(_oleobj_=SimpleNamespace(QueryInterface=lambda *args:
        (calls.append(args), dispatch)[1]))
    monkeypatch.setitem(sys.modules, 'pythoncom', SimpleNamespace(IID_IDispatch='IDispatch'))
    monkeypatch.setattr(binding, 'module', lambda: SimpleNamespace(IFixture=Interface))
    wrapped = binding.wrap(native, 'IFixture')
    assert isinstance(wrapped, Interface) and wrapped.value is dispatch
    assert calls[0][0] == Interface.CLSID


def test_none_and_offline_objects_never_load_com_bindings(monkeypatch):
    monkeypatch.setattr(binding, 'module', lambda *args: pytest.fail('Offline object loaded SDK bindings.'))
    value = SimpleNamespace(Name='offline object')
    assert binding.wrap(None, 'IFeature') is None
    assert binding.wrap(value, 'IFeature') is value


def test_missing_native_constant_has_a_stable_error(monkeypatch):
    monkeypatch.setattr(binding, 'module', lambda name: SimpleNamespace(constants=SimpleNamespace()))
    with pytest.raises(CadError) as caught:
        binding.constant('swNotAnInstalledConstant')
    assert caught.value.code == 'NATIVE_CONSTANT_UNAVAILABLE'


def test_cad_error_cannot_be_overridden_into_a_success():
    error = CadError('SAVE_FAILED', 'The save failed.', ok=True, error='hidden cause')
    result = error.result()
    assert result['ok'] is False and result['code'] == 'SAVE_FAILED'
    assert result['error'] == 'The save failed.'


def test_native_error_evidence_is_valid_strict_json_without_com_objects():
    result = CadError('NATIVE_ERROR', 'SDK failure', native=(7, float('nan'), object()),
                      values={1: float('inf')}).result()
    serialized = json.dumps(result, allow_nan=False)
    assert 'unserializable_type' in serialized
    assert result['native'][:2] == [7, 'nan']
    assert result['values'] == {'1': 'inf'}
