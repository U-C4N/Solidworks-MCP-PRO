from pathlib import Path
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest
from PIL import Image

from solidworks_mcp_pro import visualize_bridge as bridge


@pytest.fixture(autouse=True)
def isolated_renderer_process_guard(monkeypatch):
    # Real-COM routing doubles must never inspect the user's actual renderer,
    # especially while a separate live animation job is using SOLIDWORKS.
    from solidworks_mcp_pro import renderer_process

    monkeypatch.setattr(renderer_process, "wait_for_renderer_idle", lambda: {
        "ok": True, "status": "renderer_idle", "elapsed_ms": 0.0,
        "observed_pids": [], "remaining_processes": [], "stable_absence_ms": 500.0,
        "native_render_started": False, "processes_terminated": False,
    })


class FakeManager:
    def __init__(self):
        self.RenderOptions = SimpleNamespace(
            ImageFormat=0, FrameCount=10, Width=100, Height=100,
            JobName="previous", OutputFolder="previous-folder",
            DenoiserEnabled=True, TerminationMode=0, IncludeAlpha=True,
        )
        self.render_calls = 0
        self.export_calls = []
        self.behavior = "success"

    def Render(self):
        self.render_calls += 1
        if self.behavior == "exception":
            raise RuntimeError("renderer disconnected")
        if self.behavior == "false":
            return False
        if self.behavior == "missing":
            return True
        options = self.RenderOptions
        folder = Path(options.OutputFolder) / options.JobName
        folder.mkdir()
        fmt = "PNG" if options.ImageFormat == 3 else "JPEG"
        target = folder / (options.JobName + (".png" if fmt == "PNG" else ".jpeg"))
        size = (options.Width, options.Height) if self.behavior != "wrong_size" else (1, 1)
        if self.behavior == "corrupt":
            target.write_bytes(b"not an image")
        else:
            Image.new("RGB", size, "red").save(target, fmt)
        return True

    def SaveWithPartGrouping(self, path, grouping):
        self.export_calls.append((path, grouping))
        Path(path).write_bytes(b"fake project data")

    def Save(self, path):
        self.export_calls.append((path, "legacy"))
        Path(path).write_bytes(b"fake project data")


class FakeApplication:
    RevisionNumber = "34.0.0"
    ActiveDoc = object()

    def __init__(self):
        self.manager = FakeManager()
        self.addin = SimpleNamespace(GetAddinManager=lambda: self.manager, GetAddinManager2=lambda: self.manager)

    def GetAddInObject(self, prog_id):
        assert prog_id == bridge.PROG_ID
        return self.addin


@pytest.mark.parametrize("image_format", ["png", "jpeg"])
def test_render_verifies_real_image_and_restores_options(tmp_path, image_format):
    app = FakeApplication()
    original = vars(app.manager.RenderOptions).copy()
    result = bridge.render(app, tmp_path / "run", width=128, height=96, passes=20, image_format=image_format)
    assert result["ok"]
    assert result["renderer"] == "SOLIDWORKS Visualize"
    assert len(result["images"]) == 1
    assert len(result["images"][0]["sha256"]) == 64
    assert result["images"][0]["width"] == 128
    assert vars(app.manager.RenderOptions) == original


@pytest.mark.parametrize("behavior,code", [
    ("false", "RENDER_FAILED"), ("missing", "RENDER_OUTPUT_MISSING"),
    ("wrong_size", "RENDER_OUTPUT_INVALID"), ("corrupt", "RENDER_OUTPUT_INVALID"),
    ("exception", "renderer disconnected"),
])
def test_failed_render_never_reports_success_and_restores(tmp_path, behavior, code):
    app = FakeApplication()
    app.manager.behavior = behavior
    original = vars(app.manager.RenderOptions).copy()
    with pytest.raises(RuntimeError, match=code):
        bridge.render(app, tmp_path / "run", width=64, height=64)
    assert vars(app.manager.RenderOptions) == original


@pytest.mark.parametrize("arguments", [
    {"width": 0}, {"height": True}, {"passes": 0}, {"passes": 1.5},
    {"image_format": "tiff"}, {"job_name": "../escape"}, {"job_name": "CON"}, {"denoiser": 1},
])
def test_invalid_options_do_not_call_renderer(tmp_path, arguments):
    app = FakeApplication()
    with pytest.raises(bridge.VisualizeError, match="INVALID_ARGUMENT"):
        bridge.render(app, tmp_path / "run", **arguments)
    assert app.manager.render_calls == 0
    assert not (tmp_path / "run").exists()


def test_existing_outputs_are_not_overwritten(tmp_path):
    app = FakeApplication()
    (tmp_path / "valuable.txt").write_text("keep")
    with pytest.raises(bridge.VisualizeError, match="OUTPUT_EXISTS"):
        bridge.render(app, tmp_path)
    assert (tmp_path / "valuable.txt").read_text() == "keep"
    assert app.manager.render_calls == 0


def test_unsupported_or_missing_addin_is_actionable():
    app = FakeApplication()
    app.RevisionNumber = "32.0.0"
    assert bridge.status(app)["code"] == "VISUALIZE_VERSION_UNSUPPORTED"
    app.RevisionNumber = "34.0.0"
    app.addin = None
    assert bridge.status(app)["code"] == "VISUALIZE_UNAVAILABLE"


def test_status_does_not_claim_license_checked():
    result = bridge.status(FakeApplication())
    assert result["available"]
    assert result["license_verified"] is False


def test_export_2026_preserves_requested_grouping(tmp_path):
    app = FakeApplication()
    target = tmp_path / "sample.svpj"
    result = bridge.export_project(app, target, grouping="part")
    assert result["ok"]
    assert app.manager.export_calls == [(str(target), 1)]
    with pytest.raises(bridge.VisualizeError, match="OUTPUT_EXISTS"):
        bridge.export_project(app, target)


def test_export_2025_legacy_and_reject_unsupported_grouping(tmp_path):
    app = FakeApplication()
    app.RevisionNumber = "33.0.0"
    result = bridge.export_project(app, tmp_path / "legacy.svpj")
    assert result["api"] == "IVisualizeAddinManager.Save"
    with pytest.raises(bridge.VisualizeError, match="VISUALIZE_VERSION_UNSUPPORTED"):
        bridge.export_project(app, tmp_path / "parts.svpj", grouping="part")


def test_requires_active_model(tmp_path):
    app = FakeApplication()
    app.ActiveDoc = None
    with pytest.raises(bridge.VisualizeError, match="NO_ACTIVE_DOCUMENT"):
        bridge.render(app, tmp_path / "run")
    assert app.manager.render_calls == 0


@pytest.mark.parametrize("codebase,expected", [
    ("file:///C:/Program%20Files/SOLIDWORKS%20Corp/SOLIDWORKS%20Visualize/SolidWorks.Visualize.Implementation.DLL",
     r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS Visualize\SolidWorks.Visualize.Implementation.DLL"),
    ("file:///D:/CAD/SolidWorks.Visualize.Implementation.dll", r"D:\CAD\SolidWorks.Visualize.Implementation.dll"),
])
def test_visualize_codebase_decodes_local_file_uri(codebase, expected):
    assert bridge._codebase_path(codebase) == expected


@pytest.mark.parametrize("codebase", [
    "https://example.com/SolidWorks.Visualize.Implementation.DLL",
    "file://server/share/SolidWorks.Visualize.Implementation.DLL",
    "file:////server/share/SolidWorks.Visualize.Implementation.DLL",
    "file:///C:relative/SolidWorks.Visualize.Implementation.DLL",
    "file:///relative/SolidWorks.Visualize.Implementation.DLL",
    "file:///C:/CAD/other.dll",
    "file:///C:/CAD/SolidWorks.Visualize.Implementation.DLL?argument=1",
    "file:///C:/CAD/SolidWorks.Visualize.Implementation.DLL#fragment",
    "file:///C:/CAD/%00/SolidWorks.Visualize.Implementation.DLL",
    "file:///%5C%5Cserver/share/SolidWorks.Visualize.Implementation.DLL",
])
def test_visualize_codebase_refuses_arbitrary_or_remote_dll(codebase):
    with pytest.raises(bridge.VisualizeError, match="VISUALIZE_REGISTRATION_INVALID"):
        bridge._codebase_path(codebase)


def test_enable_already_available_never_loads_or_reads_registry(monkeypatch):
    app = FakeApplication()
    monkeypatch.setattr(bridge, "_installed_addin_path", lambda: pytest.fail("should not inspect registration"))
    result = bridge.enable(app)
    assert result["available"]
    assert result["already_loaded"]
    assert result["load_code"] is None


@pytest.mark.parametrize("load_code", [0, 2])
def test_enable_loads_only_discovered_path_then_probes(tmp_path, monkeypatch, load_code):
    app = FakeApplication()
    addin = app.addin
    app.addin = None
    path = tmp_path / bridge.ADDIN_FILENAME
    path.write_bytes(b"fixture")
    monkeypatch.setattr(bridge, "_installed_addin_path", lambda: path)
    calls = []

    def load(argument):
        calls.append(argument)
        app.addin = addin
        return load_code

    app.LoadAddIn = load
    result = bridge.enable(app)
    assert calls == [str(path)]
    assert result["available"]
    assert result["load_code"] == load_code
    assert result["license_verified"] is False


def test_enable_false_probe_after_successful_load_stays_unavailable(tmp_path, monkeypatch):
    app = FakeApplication()
    app.addin = None
    monkeypatch.setattr(bridge, "_installed_addin_path", lambda: tmp_path / bridge.ADDIN_FILENAME)
    app.LoadAddIn = lambda path: 0
    result = bridge.enable(app)
    assert result["ok"] is False
    assert result["available"] is False
    assert result["load_code"] == 0


@pytest.mark.parametrize("load_code", [-1, 1, 3, 4, 5, 6, 7, 100])
def test_enable_reports_load_errors(tmp_path, monkeypatch, load_code):
    app = FakeApplication()
    app.addin = None
    monkeypatch.setattr(bridge, "_installed_addin_path", lambda: tmp_path / bridge.ADDIN_FILENAME)
    app.LoadAddIn = lambda path: load_code
    result = bridge.enable(app)
    assert result["available"] is False
    assert result["code"] == "VISUALIZE_LOAD_FAILED"
    assert result["load_code"] == load_code


def test_enable_invalid_registration_never_loads(monkeypatch):
    app = FakeApplication()
    app.addin = None

    def invalid():
        raise bridge.VisualizeError("VISUALIZE_NOT_INSTALLED", "no registration")

    monkeypatch.setattr(bridge, "_installed_addin_path", invalid)
    app.LoadAddIn = lambda path: pytest.fail("must not load")
    assert bridge.enable(app)["code"] == "VISUALIZE_NOT_INSTALLED"


def _mock_vtable_client(monkeypatch, *, active_pid=123):
    manager = FakeManager()
    calls = []
    interface1, interface2 = object(), object()
    addin = SimpleNamespace(GetAddinManager=lambda: manager, GetAddinManager2=lambda: manager)

    def query(interface):
        calls.append(interface)
        return addin

    raw = SimpleNamespace(QueryInterface=query)
    app = SimpleNamespace(GetProcessID=active_pid,
                          GetAddInObject=lambda progid: SimpleNamespace(_comobj=raw))
    client = ModuleType("comtypes.client")
    client.GetModule = lambda spec: SimpleNamespace(IVisualizeAddin=interface1, IVisualizeAddin2=interface2)
    client.GetActiveObject = lambda progid, dynamic: app
    package = ModuleType("comtypes")
    package.client = client
    monkeypatch.setitem(sys.modules, "comtypes", package)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    return manager, calls, interface1, interface2


@pytest.mark.parametrize("version2", [False, True])
def test_vtable_getter_queries_correct_interface(monkeypatch, version2):
    manager, calls, interface1, interface2 = _mock_vtable_client(monkeypatch)
    app = SimpleNamespace(GetProcessID=lambda: 123)
    assert bridge._vtable_manager(app, version2) is manager
    assert calls == [interface2 if version2 else interface1]


def test_vtable_refuses_different_solidworks_instance(monkeypatch):
    _, calls, _, _ = _mock_vtable_client(monkeypatch, active_pid=456)
    app = SimpleNamespace(GetProcessID=lambda: 123)
    with pytest.raises(bridge.VisualizeError, match="SOLIDWORKS_INSTANCE_MISMATCH"):
        bridge._vtable_manager(app, False)
    assert calls == []


def test_typed_vtable_pointer_is_never_rewrapped_as_dispatch():
    class VtablePointer:
        _iid_ = "interface-guid"

        def QueryInterface(self, *args):
            pytest.fail("already typed pointer must not be queried with pywin32 GUIDs")

    pointer = VtablePointer()
    assert bridge._typed(pointer, "IRenderOptions") is pointer


@pytest.mark.parametrize("return_value", [None, 0])
def test_open_project_uses_manager2_without_active_model_and_does_not_claim_loaded(tmp_path, monkeypatch, return_value):
    from solidworks_mcp_pro import visualizelaunch

    app = FakeApplication()
    app.ActiveDoc = None
    project = tmp_path / "project.svpj"
    calls = []
    monkeypatch.setattr(visualizelaunch, "_project_file", lambda path: project)
    app.manager.LaunchVisualize = lambda path: calls.append(path) or return_value
    app.addin.GetAddinManager = lambda: pytest.fail("must use manager2")
    result = bridge.open_project(app, str(project))
    assert result["ok"] and result["status"] == "launch_requested"
    assert result["project_loaded"] is None
    assert result["api"] == "IVisualizeAddinManager2.LaunchVisualize"
    assert calls == [str(project)]


@pytest.mark.parametrize("return_value", [False, True, -1, 1, "success"])
def test_open_project_does_not_accept_unexpected_native_result(tmp_path, monkeypatch, return_value):
    from solidworks_mcp_pro import visualizelaunch

    app = FakeApplication()
    monkeypatch.setattr(visualizelaunch, "_project_file", lambda path: tmp_path / "project.svpj")
    app.manager.LaunchVisualize = lambda path: return_value
    with pytest.raises(bridge.VisualizeError, match="VISUALIZE_LAUNCH_UNEXPECTED_RESULT"):
        bridge.open_project(app, "project.svpj")


def test_open_project_validates_before_addin_access(monkeypatch):
    from solidworks_mcp_pro import visualizelaunch

    def invalid(path):
        raise visualizelaunch.VisualizeLaunchError("INVALID_PROJECT_PATH", "missing project")

    monkeypatch.setattr(visualizelaunch, "_project_file", invalid)
    monkeypatch.setattr(bridge, "_manager", lambda *args, **kwargs: pytest.fail("no addin access"))
    with pytest.raises(visualizelaunch.VisualizeLaunchError):
        bridge.open_project(FakeApplication(), "missing.svpj")


def test_open_project_rejects_2025_without_launch(tmp_path, monkeypatch):
    from solidworks_mcp_pro import visualizelaunch

    app = FakeApplication()
    app.RevisionNumber = "33.0.0"
    monkeypatch.setattr(visualizelaunch, "_project_file", lambda path: tmp_path / "project.svpj")
    app.manager.LaunchVisualize = lambda path: pytest.fail("unsupported version must not launch")
    with pytest.raises(bridge.VisualizeError, match="VISUALIZE_VERSION_UNSUPPORTED"):
        bridge.open_project(app, "project.svpj")


def test_open_project_native_failure_propagates(tmp_path, monkeypatch):
    from solidworks_mcp_pro import visualizelaunch

    app = FakeApplication()
    monkeypatch.setattr(visualizelaunch, "_project_file", lambda path: tmp_path / "project.svpj")

    def failed(path):
        raise RuntimeError("native HRESULT error")

    app.manager.LaunchVisualize = failed
    with pytest.raises(RuntimeError, match="HRESULT"):
        bridge.open_project(app, "project.svpj")


def test_render_timings_separate_native_work_verification_and_restoration(tmp_path, monkeypatch):
    app = FakeApplication()
    original = vars(app.manager.RenderOptions).copy()
    clock = {"now": 10.0, "restoring": False}

    class TimedOptions(SimpleNamespace):
        def __setattr__(self, key, value):
            if clock["restoring"]:
                clock["now"] += 0.1
            super().__setattr__(key, value)

    app.manager.RenderOptions = TimedOptions(**original)
    native_render = app.manager.Render
    verify_images = bridge._verify_images

    def manager(*args, **kwargs):
        clock["now"] += 2.0
        return app.manager

    def render():
        clock["now"] += 5.0
        return native_render()

    def verify(*args, **kwargs):
        result = verify_images(*args, **kwargs)
        clock["now"] += 0.5
        clock["restoring"] = True
        return result

    monkeypatch.setattr(bridge.time, "perf_counter", lambda: clock["now"])
    monkeypatch.setattr(bridge, "_manager", manager)
    monkeypatch.setattr(bridge, "_verify_images", verify)
    app.manager.Render = render
    result = bridge.render(app, tmp_path / "run", width=64, height=64)
    assert result["timings_ms"] == {"setup": 2000.0, "native_render": 5000.0,
        "verify_output": 500.0, "restore_options": 900.0, "bridge_total": 8400.0}
    assert "not measured GPU kernel time" in result["timing_note"]
    assert vars(app.manager.RenderOptions) == original


def test_real_com_render_routes_through_fixed_macro_and_keeps_verification(tmp_path, monkeypatch):
    from solidworks_mcp_pro import visualize_macro

    app = FakeApplication()
    app._oleobj_ = object()  # Production routing discriminator; no COM object.
    original = vars(app.manager.RenderOptions).copy()
    monkeypatch.setattr(bridge, "_manager", lambda *args, **kwargs: app.manager)
    monkeypatch.setattr(bridge, "_client_apartment", lambda: 0)
    app.manager.Render = lambda: pytest.fail("external manager.Render must not be called")
    calls = []

    def fixed_render(actual_app, folder):
        calls.append((actual_app, folder))
        options = app.manager.RenderOptions
        assert options.Width == 128 and options.Height == 96 and options.FrameCount == 20
        Image.new("RGB", (128, 96), "blue").save(folder / "render.png")
        return {"accepted": True, "marker_path": "test-marker"}

    monkeypatch.setattr(visualize_macro, "render_on_ui", fixed_render)
    result = bridge.render(app, tmp_path / "run", width=128, height=96, passes=20)
    assert result["ok"] and result["execution"] == "solidworks_in_process_macro"
    assert result["macro_evidence"]["client_apartment"] == 0
    assert result["images"][0]["width"] == 128 and result["images"][0]["sha256"]
    assert calls == [(app, tmp_path / "run")]
    assert vars(app.manager.RenderOptions) == original


def test_fixed_macro_render_failure_still_restores_native_options(tmp_path, monkeypatch):
    from solidworks_mcp_pro import visualize_macro

    app = FakeApplication()
    app._oleobj_ = object()
    original = vars(app.manager.RenderOptions).copy()
    monkeypatch.setattr(bridge, "_manager", lambda *args, **kwargs: app.manager)
    monkeypatch.setattr(bridge, "_client_apartment", lambda: 0)

    def failed(*args):
        raise visualize_macro.VisualizeMacroError("VISUALIZE_MACRO_INCOMPLETE", "native render failed")

    monkeypatch.setattr(visualize_macro, "render_on_ui", failed)
    with pytest.raises(visualize_macro.VisualizeMacroError):
        bridge.render(app, tmp_path / "run", width=64, height=64)
    assert vars(app.manager.RenderOptions) == original


@pytest.mark.parametrize("grouping, expected", [("appearance", 0), ("part", 1), ("none", 2)])
def test_real_com_export_uses_unique_fixed_macro_and_verifies_file(tmp_path, monkeypatch, grouping, expected):
    from solidworks_mcp_pro import visualize_macro

    app = FakeApplication()
    app._oleobj_ = object()
    monkeypatch.setattr(bridge, "_client_apartment", lambda: 0)
    monkeypatch.setattr(bridge, "_manager", lambda *args, **kwargs: pytest.fail("no external export call"))
    calls = []

    def fixed_export(actual_app, target, mode, scratch):
        calls.append((actual_app, target, mode, scratch))
        assert scratch.is_dir() and scratch.parent == target.parent and scratch != target.parent
        target.write_bytes(b"native-project")
        return {"accepted": True}

    monkeypatch.setattr(visualize_macro, "export_on_ui", fixed_export)
    target = tmp_path / "model.svpj"
    result = bridge.export_project(app, target, grouping)
    assert result["ok"] and result["execution"] == "solidworks_in_process_macro"
    assert result["macro_evidence"]["client_apartment"] == 0
    assert result["bytes"] == len(b"native-project") and len(result["sha256"]) == 64
    assert calls[0][:3] == (app, target, expected)
    bridge.export_project(app, tmp_path / "second.svpj", grouping)
    assert calls[0][3] != calls[1][3]


def test_macro_export_acceptance_without_output_is_not_success(tmp_path, monkeypatch):
    from solidworks_mcp_pro import visualize_macro

    app = FakeApplication()
    app._oleobj_ = object()
    monkeypatch.setattr(bridge, "_client_apartment", lambda: 0)
    monkeypatch.setattr(visualize_macro, "export_on_ui", lambda *args: {"accepted": True})
    with pytest.raises(bridge.VisualizeError, match="PROJECT_OUTPUT_MISSING"):
        bridge.export_project(app, tmp_path / "missing.svpj", "part")


def _native_route_double(monkeypatch):
    app = FakeApplication()
    app._oleobj_ = object()
    monkeypatch.setattr(bridge, "_manager", lambda *args, **kwargs: app.manager)
    monkeypatch.setattr(bridge, "_client_apartment", lambda: 0)
    app.manager.Render = lambda: pytest.fail("native route must not call external manager.Render")
    return app


@pytest.mark.parametrize("code", ["VISUALIZE_RENDERER_BUSY", "RENDERER_PROCESS_INSPECTION_FAILED"])
def test_native_renderer_guard_failure_blocks_addin_options_and_macro(tmp_path, monkeypatch, code):
    from solidworks_mcp_pro import renderer_process, visualize_macro

    app = _native_route_double(monkeypatch)
    original = vars(app.manager.RenderOptions).copy()
    failure = {"ok": False, "code": code, "elapsed_ms": 60000, "remaining_processes": [{"pid": 42}]}
    monkeypatch.setattr(renderer_process, "wait_for_renderer_idle", lambda: failure)
    monkeypatch.setattr(bridge, "_manager", lambda *args, **kwargs: pytest.fail("guard must precede addin access"))
    monkeypatch.setattr(visualize_macro, "render_on_ui", lambda *args: pytest.fail("guard must block native macro"))
    with pytest.raises(bridge.VisualizeError, match=code):
        bridge.render(app, tmp_path / "run", width=64, height=64)
    assert vars(app.manager.RenderOptions) == original
    assert not (tmp_path / "run").exists()


def test_native_guard_orders_idle_before_setup_and_cleanup_after_verified_image(tmp_path, monkeypatch):
    from solidworks_mcp_pro import renderer_process, visualize_macro

    app = _native_route_double(monkeypatch)
    original = vars(app.manager.RenderOptions).copy()
    events, reports = [], []
    verify = bridge._verify_images

    class TrackedOptions(SimpleNamespace):
        def __setattr__(self, name, value):
            if name == "IncludeAlpha" and value is True:
                events.append("restore")
            super().__setattr__(name, value)

    app.manager.RenderOptions = TrackedOptions(**original)

    def guard():
        events.append("before_guard" if not reports else "after_guard")
        report = {"ok": True, "elapsed_ms": 500.0 if not reports else 700.0,
                  "observed_pids": [] if not reports else [123], "processes_terminated": False}
        if reports:
            assert events[-2] == "verified_image"
            assert app.manager.RenderOptions.Width == 64
        else:
            assert vars(app.manager.RenderOptions) == original
        reports.append(report)
        return report

    def manager(*args, **kwargs):
        events.append("manager")
        return app.manager

    def macro(actual_app, folder):
        events.append("macro")
        assert app.manager.RenderOptions.Width == 64
        Image.new("RGB", (64, 64), "blue").save(folder / "render.png")
        return {"accepted": True}

    def checked_images(*args, **kwargs):
        result = verify(*args, **kwargs)
        events.append("verified_image")
        return result

    monkeypatch.setattr(renderer_process, "wait_for_renderer_idle", guard)
    monkeypatch.setattr(bridge, "_manager", manager)
    monkeypatch.setattr(bridge, "_verify_images", checked_images)
    monkeypatch.setattr(visualize_macro, "render_on_ui", macro)
    result = bridge.render(app, tmp_path / "run", width=64, height=64)
    assert events == ["before_guard", "manager", "macro", "verified_image", "after_guard", "restore"]
    assert result["renderer_process_guards"] == {"before_render": reports[0], "after_render": reports[1]}
    assert result["timings_ms"]["renderer_idle_wait"] == 500
    assert result["timings_ms"]["renderer_cleanup_wait"] == 700
    assert vars(app.manager.RenderOptions) == original


@pytest.mark.parametrize("code", ["VISUALIZE_RENDERER_BUSY", "RENDERER_PROCESS_INSPECTION_FAILED"])
def test_cleanup_guard_failure_never_reports_render_success_and_restores_options(tmp_path, monkeypatch, code):
    from solidworks_mcp_pro import renderer_process, visualize_macro

    app = _native_route_double(monkeypatch)
    original = vars(app.manager.RenderOptions).copy()
    calls = []
    results = iter([{"ok": True, "elapsed_ms": 500}, {"ok": False, "code": code, "elapsed_ms": 60000}])
    monkeypatch.setattr(renderer_process, "wait_for_renderer_idle", lambda: next(results))

    def macro(actual_app, folder):
        calls.append(folder)
        Image.new("RGB", (64, 64), "blue").save(folder / "render.png")
        return {"accepted": True}

    monkeypatch.setattr(visualize_macro, "render_on_ui", macro)
    with pytest.raises(bridge.VisualizeError, match=code):
        bridge.render(app, tmp_path / "run", width=64, height=64)
    assert calls == [tmp_path / "run"]  # No automatic render retry.
    assert (tmp_path / "run" / "render.png").is_file()
    assert vars(app.manager.RenderOptions) == original


def test_sequential_native_renders_each_wait_before_and_after_macro(tmp_path, monkeypatch):
    from solidworks_mcp_pro import renderer_process, visualize_macro

    app = _native_route_double(monkeypatch)
    events = []

    def guard():
        events.append("guard")
        return {"ok": True, "elapsed_ms": 500}

    def macro(actual_app, folder):
        events.append("macro")
        Image.new("RGB", (64, 64), "blue").save(folder / "render.png")
        return {"accepted": True}

    monkeypatch.setattr(renderer_process, "wait_for_renderer_idle", guard)
    monkeypatch.setattr(visualize_macro, "render_on_ui", macro)
    first = bridge.render(app, tmp_path / "first", width=64, height=64)
    second = bridge.render(app, tmp_path / "second", width=64, height=64)
    assert first["ok"] and second["ok"]
    assert events == ["guard", "macro", "guard", "guard", "macro", "guard"]


def test_failed_macro_does_not_let_next_render_bypass_process_guard(tmp_path, monkeypatch):
    from solidworks_mcp_pro import renderer_process, visualize_macro

    app = _native_route_double(monkeypatch)
    original = vars(app.manager.RenderOptions).copy()
    guard_results = iter([{"ok": True, "elapsed_ms": 500},
                          {"ok": False, "code": "VISUALIZE_RENDERER_BUSY", "elapsed_ms": 60000}])
    monkeypatch.setattr(renderer_process, "wait_for_renderer_idle", lambda: next(guard_results))
    calls = []

    def failed(actual_app, folder):
        calls.append(folder)
        raise visualize_macro.VisualizeMacroError("VISUALIZE_MACRO_INCOMPLETE", "renderer may still exist")

    monkeypatch.setattr(visualize_macro, "render_on_ui", failed)
    with pytest.raises(visualize_macro.VisualizeMacroError):
        bridge.render(app, tmp_path / "first", width=64, height=64)
    with pytest.raises(bridge.VisualizeError, match="VISUALIZE_RENDERER_BUSY"):
        bridge.render(app, tmp_path / "second", width=64, height=64)
    assert calls == [tmp_path / "first"] and not (tmp_path / "second").exists()
    assert vars(app.manager.RenderOptions) == original


def test_non_native_route_never_uses_windows_renderer_process_guard(tmp_path, monkeypatch):
    from solidworks_mcp_pro import renderer_process

    monkeypatch.setattr(renderer_process, "wait_for_renderer_idle", lambda: pytest.fail("mock renderer must stay offline"))
    result = bridge.render(FakeApplication(), tmp_path / "run", width=64, height=64)
    assert result["ok"] and result["renderer_process_guards"] == {}
