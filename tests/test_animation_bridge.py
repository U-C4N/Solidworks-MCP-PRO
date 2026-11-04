from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import animation_bridge as bridge


class Component:
    def __init__(self, path, name):
        self.path = path
        self.Name2 = name
        self.ReferencedConfiguration = "Acik"
        self.names = ["Acik", "Basili"]

    def GetPathName(self):
        return str(self.path)

    def GetConfigurationNames(self):
        return tuple(self.names)


class Package:
    def __init__(self, paths):
        self.paths = paths
        self.target = ""
        self.bad_destination = False

    def GetDocumentNames(self, output):
        assert output is None
        return True, tuple(str(path) for path in self.paths)

    def GetDocumentNamesCount(self):
        return len(self.paths)

    def SetSaveToName(self, override, target):
        assert override is True
        self.target = target
        return True

    def GetSaveToName(self):
        return self.target

    def GetDocumentSaveToNames(self, paths=None, statuses=None):
        assert paths is None and statuses is None
        names = [str(Path(self.target) / (self.AddPrefix + path.name)) for path in self.paths]
        if self.bad_destination:
            names[-1] = str(self.paths[-1])
        return True, names, [0] * len(names)


class Extension:
    def __init__(self, paths):
        self.package = Package(paths)
        self.saves = 0
        self.statuses = None
        self.omit_file = False

    def __call__(self):
        raise AssertionError("Do not invoke a COM property value")

    def GetPackAndGo(self):
        return self.package

    def SavePackAndGo(self, package):
        self.saves += 1
        names = package.GetDocumentSaveToNames()[1]
        for index, name in enumerate(names):
            if self.omit_file and index == len(names) - 1:
                continue
            Path(name).write_bytes(b"independent native package output")
        return self.statuses if self.statuses is not None else [0] * len(names)


class Model:
    def __init__(self, path, part_paths):
        self.path = path
        self.current = "Sonuc"
        self.names = ["Sonuc", "Acik"]
        self.parts = [Component(part_paths[0], "Spring-1"), Component(part_paths[1], "Sheet-1")]
        self.children = []
        self.Extension = Extension([path, *part_paths])
        self.read_only = False
        self.view_only = False
        self.dirty = False
        self.reject_configuration = False
        self.rebuild_failures = 0
        self.rebuilds = 0
        self.shown = []

    def __call__(self):
        raise AssertionError("Do not invoke a COM property value")

    def GetPathName(self):
        return str(self.path)

    def GetType(self):
        return 2

    def IsOpenedReadOnly(self):
        return self.read_only

    def IsOpenedViewOnly(self):
        return self.view_only

    def GetSaveFlag(self):
        return self.dirty

    def GetActiveConfiguration(self):
        return SimpleNamespace(Name=self.current)

    def GetConfigurationNames(self):
        return tuple(self.names)

    def GetComponents(self, top_only):
        return tuple(self.parts if top_only else self.parts + self.children)

    def ShowConfiguration2(self, name):
        self.shown.append(name)
        if self.reject_configuration:
            return False
        self.current = name
        return True

    def EditRebuild3(self):
        self.rebuilds += 1
        if self.rebuild_failures:
            self.rebuild_failures -= 1
            return False
        return True

    def GraphicsRedraw2(self):
        pass


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "press.sldasm"
    parts = [tmp_path / "spring.sldprt", tmp_path / "sheet.sldprt"]
    for item in [path, *parts]:
        item.write_bytes(b"original CAD fixture")
    model = Model(path, parts)

    def dependencies(document, traverse, search, readonly):
        assert (traverse, search, readonly) == (True, False, False)
        names = model.Extension.package.GetDocumentSaveToNames()[1]
        assert document == names[0]
        return [value for name in names[1:] for value in (Path(name).name, name)]

    app = SimpleNamespace(ActiveDoc=model, GetDocumentDependencies2=dependencies)
    return app, path, tmp_path / "animation-clone"


def test_inspection_is_read_only_and_accepts_readonly_source(setup):
    app, path, _ = setup
    app.ActiveDoc.read_only = True
    result = bridge.inspect(app, path)
    assert result["ok"] and result["read_only"]
    assert result["configurations"] == ["Sonuc", "Acik"]
    assert result["components"][0]["configurations"] == ["Acik", "Basili"]
    assert not result["mutation_attempted"] and not result["saved"]
    assert app.ActiveDoc.rebuilds == 0


def test_switch_assembly_configuration_reads_back(setup):
    app, path, _ = setup
    result = bridge.configure(app, path, configuration="Acik")
    assert result["ok"] and result["verified"]
    assert result["configuration"] == "Acik"
    assert not result["saved"]


def test_component_configurations_no_part_document_mutation(setup):
    app, path, _ = setup
    result = bridge.configure(app, path, components=[
        {"component": "Spring-1", "configuration": "Basili"},
        {"component": "Sheet-1", "configuration": "Basili"},
    ])
    assert result["ok"]
    assert all(item["referenced_configuration"] == "Basili" for item in result["applied"])
    assert app.ActiveDoc.current == "Sonuc"
    assert app.ActiveDoc.rebuilds == 2


def test_entire_component_batch_validates_before_change(setup):
    app, path, _ = setup
    with pytest.raises(bridge.AnimationError, match="CONFIGURATION_UNKNOWN"):
        bridge.configure(app, path, components=[
            {"component": "Spring-1", "configuration": "Basili"},
            {"component": "Sheet-1", "configuration": "Missing"},
        ])
    assert all(item.ReferencedConfiguration == "Acik" for item in app.ActiveDoc.parts)
    assert app.ActiveDoc.rebuilds == 0


@pytest.mark.parametrize("kwargs", [
    {}, {"configuration": ""}, {"configuration": "Sonuc", "components": [{"component": "Spring-1", "configuration": "Basili"}]},
    {"components": "Spring-1"}, {"components": [{}]},
    {"components": [{"component": "Spring-1", "configuration": "Basili", "extra": 1}]},
    {"components": [{"component": "Spring-1", "configuration": "Acik"}] * 2},
])
def test_invalid_configure_requests(setup, kwargs):
    app, path, _ = setup
    with pytest.raises(bridge.AnimationError, match="INVALID_ARGUMENT"):
        bridge.configure(app, path, **kwargs)
    assert app.ActiveDoc.rebuilds == 0


def test_readonly_configuration_mutation_rejected(setup):
    app, path, _ = setup
    app.ActiveDoc.read_only = True
    with pytest.raises(bridge.AnimationError, match="DOCUMENT_READ_ONLY"):
        bridge.configure(app, path, configuration="Acik")


def test_different_active_assembly_rejected(setup, tmp_path):
    app, _, _ = setup
    other = tmp_path / "other.sldasm"
    other.write_bytes(b"other")
    with pytest.raises(bridge.AnimationError, match="DOCUMENT_MISMATCH"):
        bridge.inspect(app, other)


def test_nested_component_is_inspectable_but_not_configurable(setup):
    app, path, _ = setup
    child = Component(app.ActiveDoc.parts[0].path, "Subassembly-1/Nested-1")
    app.ActiveDoc.children.append(child)
    assert bridge.inspect(app, path)["components"][-1]["is_top_level"] is False
    with pytest.raises(bridge.AnimationError, match="COMPONENT_NOT_UNIQUE"):
        bridge.configure(app, path, components=[{"component": child.Name2, "configuration": "Basili"}])


def test_failed_component_rebuild_restores_prior_configuration(setup):
    app, path, _ = setup
    app.ActiveDoc.rebuild_failures = 1
    result = bridge.configure(app, path, components=[{"component": "Spring-1", "configuration": "Basili"}])
    assert not result["ok"]
    assert result["rollback"]["attempted"] and result["rollback"]["verified"]
    assert app.ActiveDoc.parts[0].ReferencedConfiguration == "Acik"
    assert not result["partial_state_possible"]


def test_failed_assembly_rebuild_restores_prior_configuration(setup):
    app, path, _ = setup
    app.ActiveDoc.rebuild_failures = 1
    result = bridge.configure(app, path, configuration="Acik")
    assert not result["ok"] and result["rollback"]["verified"]
    assert app.ActiveDoc.current == "Sonuc"


def test_restore_failure_is_reported_not_hidden(setup):
    app, path, _ = setup
    app.ActiveDoc.rebuild_failures = 2
    result = bridge.configure(app, path, components=[{"component": "Spring-1", "configuration": "Basili"}])
    assert not result["ok"] and result["partial_state_possible"]
    assert not result["rollback"]["verified"]


def test_clone_writes_new_prefixed_files_and_verifies_isolation(setup):
    app, path, output = setup
    originals = {str(item): item.read_bytes() for item in [path, *(part.path for part in app.ActiveDoc.parts)]}
    result = bridge.clone(app, path, output, prefix="Video_")
    assert result["ok"] and result["reference_isolation_verified"]
    assert result["assembly_path"] == str(output / "Video_press.sldasm")
    assert result["native_statuses"] == [0, 0, 0]
    assert all(Path(item["destination"]).is_relative_to(output) for item in result["mapping"])
    assert not result["opened"] and not result["source_saved"]
    assert all(Path(name).read_bytes() == content for name, content in originals.items())
    assert app.ActiveDoc.GetPathName() == str(path)


def test_clone_readonly_source_supported(setup):
    app, path, output = setup
    app.ActiveDoc.read_only = True
    assert bridge.clone(app, path, output)["ok"]


def test_clone_unsaved_source_refused_without_saving(setup):
    app, path, output = setup
    app.ActiveDoc.dirty = True
    with pytest.raises(bridge.AnimationError, match="SOURCE_UNSAVED"):
        bridge.clone(app, path, output)
    assert not output.exists()


def test_clone_existing_even_empty_directory_refused(setup):
    app, path, output = setup
    output.mkdir()
    with pytest.raises(bridge.AnimationError, match="OUTPUT_EXISTS_OR_INVALID"):
        bridge.clone(app, path, output)
    assert app.ActiveDoc.Extension.saves == 0


@pytest.mark.parametrize("prefix", ["", "../outside", "bad/name", "a" * 49, "Türkçe", None])
def test_bad_prefix_rejected_before_native_save(setup, prefix):
    app, path, output = setup
    with pytest.raises(bridge.AnimationError, match="INVALID_ARGUMENT"):
        bridge.clone(app, path, output, prefix=prefix)
    assert not output.exists()


def test_native_destination_outside_target_is_preflight_failure(setup):
    app, path, output = setup
    app.ActiveDoc.Extension.package.bad_destination = True
    with pytest.raises(bridge.AnimationError, match="isolated mapping"):
        bridge.clone(app, path, output)
    assert not output.exists() and app.ActiveDoc.Extension.saves == 0


def test_flattened_name_collision_is_rejected_before_write(setup, tmp_path):
    app, path, output = setup
    other_folder = tmp_path / "nested"
    other_folder.mkdir()
    duplicate = other_folder / "spring.sldprt"
    duplicate.write_bytes(b"other spring")
    app.ActiveDoc.Extension.package.paths.append(duplicate)
    with pytest.raises(bridge.AnimationError, match="NAME_COLLISION"):
        bridge.clone(app, path, output)
    assert not output.exists()


def test_pack_and_go_native_failure_returns_partial_files_evidence(setup):
    app, path, output = setup
    app.ActiveDoc.Extension.statuses = [0, 0, 4]
    result = bridge.clone(app, path, output)
    assert not result["ok"] and result["partial_files_possible"]
    assert result["native_statuses"] == [0, 0, 4]


def test_missing_saved_file_cannot_be_success(setup):
    app, path, output = setup
    app.ActiveDoc.Extension.omit_file = True
    result = bridge.clone(app, path, output)
    assert not result["ok"]
    assert result["failed"]["stage"] == "saved_file_verification"


def test_old_external_reference_is_detected_after_native_save(setup):
    app, path, output = setup
    source = app.ActiveDoc.parts[0].path
    app.GetDocumentDependencies2 = lambda *_: [source.name, str(source)]
    result = bridge.clone(app, path, output)
    assert not result["ok"]
    assert result["failed"]["stage"] == "saved_reference_isolation"
    assert "external or missing reference" in result["failed"]["error"]


def test_callable_dispatch_property_values_not_invoked(setup):
    app, path, output = setup
    assert callable(app.ActiveDoc) and callable(app.ActiveDoc.Extension)
    assert bridge.inspect(app, path)["ok"]
    assert bridge.clone(app, path, output)["ok"]


@pytest.fixture
def vtable_setup(setup, monkeypatch):
    app, path, output = setup
    module = SimpleNamespace(ISldWorks=object(), IModelDoc2=object())
    installed = SimpleNamespace(CLSID="{83A33D31-27C5-11CE-BFD4-00400513BB57}", MajorVersion=34, MinorVersion=0)
    calls = []
    model = app.ActiveDoc
    app._oleobj_ = object()
    app.GetProcessID = lambda: 501
    model.pack_calls = 0

    def get_package():
        model.pack_calls += 1
        return model.Extension.package

    model.Extension.GetPackAndGo = get_package

    def query_interface(interface):
        assert interface is module.IModelDoc2
        calls.append("model_qi")
        return model

    native_app = SimpleNamespace(GetProcessID=lambda: 501,
                                 ActiveDoc=SimpleNamespace(QueryInterface=query_interface))

    def get_module(identity):
        assert identity == (installed.CLSID, 34, 0)
        calls.append("load_typelib")
        return module

    def get_active(progid, interface):
        assert progid == "SldWorks.Application" and interface is module.ISldWorks
        calls.append("get_existing_typed_app")
        return native_app

    client = SimpleNamespace(GetModule=get_module, GetActiveObject=get_active)
    monkeypatch.setitem(sys.modules, "comtypes", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    from solidworks_mcp_pro.native import binding
    monkeypatch.setattr(binding, "module", lambda: installed)
    return app, path, output, native_app, model, calls


def test_pack_vtable_attaches_exact_existing_instance(vtable_setup):
    app, path, _, _, model, calls = vtable_setup
    extension, package, vtable = bridge._pack_objects(app, model, path)
    assert extension is model.Extension and package is model.Extension.package and vtable is True
    assert calls == ["load_typelib", "get_existing_typed_app", "model_qi"]
    assert model.pack_calls == 1


@pytest.mark.parametrize("failure,code", [
    ("pid", "INSTANCE_MISMATCH"), ("no_document", "NO_ACTIVE_DOCUMENT"),
    ("path", "DOCUMENT_MISMATCH"), ("type", "ASSEMBLY_REQUIRED"),
    ("view_only", "DOCUMENT_VIEW_ONLY"), ("dirty", "SOURCE_UNSAVED"),
])
def test_pack_vtable_revalidates_before_package_call(vtable_setup, tmp_path, failure, code):
    app, path, _, native_app, model, _ = vtable_setup
    if failure == "pid":
        native_app.GetProcessID = lambda: 502
    elif failure == "no_document":
        native_app.ActiveDoc = None
    elif failure == "path":
        model.path = tmp_path / "another.sldasm"
        model.path.write_bytes(b"another assembly")
    elif failure == "type":
        model.GetType = lambda: 1
    elif failure == "view_only":
        model.view_only = True
    elif failure == "dirty":
        model.dirty = True
    with pytest.raises(bridge.AnimationError, match=code):
        bridge._pack_objects(app, model, path)
    assert model.pack_calls == 0


def test_pack_vtable_does_not_fall_back_to_dispatch(vtable_setup):
    app, path, _, _, model, _ = vtable_setup

    def failed():
        raise RuntimeError("native failure")

    model.Extension.GetPackAndGo = failed
    with pytest.raises(bridge.AnimationError, match="vtable GetPackAndGo.*native failure"):
        bridge._pack_objects(app, model, path)


@pytest.mark.parametrize("native,out_count,expected", [
    ((("a.sldasm", "b.sldprt"), True), 1, (True, ("a.sldasm", "b.sldprt"))),
    ((("a.sldasm",), (0,), True), 2, (True, ("a.sldasm",), (0,))),
])
def test_pack_vtable_output_order_and_no_placeholder_arguments(native, out_count, expected):
    assert bridge._pack_outputs("native", lambda: native, out_count, True) == expected


@pytest.mark.parametrize("native,out_count", [
    ((("a.sldasm",), False), 1), (True, 1),
    ((True, ("a.sldasm",)), 1), ((("a.sldasm",), (0,), False), 2),
])
def test_pack_vtable_bad_outputs_fail_closed(native, out_count):
    with pytest.raises(bridge.AnimationError, match="PACK_AND_GO_PREFLIGHT_FAILED"):
        bridge._pack_outputs("native", lambda: native, out_count, True)


def test_clone_through_vtable_contract_preserves_all_file_checks(setup, monkeypatch):
    app, path, output = setup
    extension = app.ActiveDoc.Extension
    package = extension.package
    names = package.GetDocumentNames
    destinations = package.GetDocumentSaveToNames
    package.GetDocumentNames = lambda: (names(None)[1], True)
    package.GetDocumentSaveToNames = lambda: (*destinations()[1:], True)

    def save(native_package):
        assert native_package is package
        extension.saves += 1
        files = package.GetDocumentSaveToNames()[0]
        for name in files:
            Path(name).write_bytes(b"isolated vtable fixture")
        return tuple(0 for _ in files)

    extension.SavePackAndGo = save
    app.GetDocumentDependencies2 = lambda *_: [
        value for name in package.GetDocumentSaveToNames()[0][1:] for value in (Path(name).name, name)
    ]
    monkeypatch.setattr(bridge, "_pack_objects", lambda *_: (extension, package, True))
    result = bridge.clone(app, path, output)
    assert result["ok"] and result["reference_isolation_verified"]
    assert result["native_statuses"] == [0, 0, 0] and extension.saves == 1
