from __future__ import annotations

import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile
from types import SimpleNamespace

from PIL import Image
import pytest

from solidworks_mcp_pro import project_render_bridge as bridge


def package(path: Path, *, guid="camera-source", position="1", extra=None, parent=None):
    matrix = ('<A X="1" Y="0" Z="0"/><B X="0" Y="1" Z="0"/>'
              '<C X="0" Y="0" Z="1"/>')
    project = f'''<?xml version="1.0"?>
    <ProjectDocument xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xmlns:xsd="http://www.w3.org/2001/XMLSchema" Version="34.01">
      <BaseNode xsi:type="CameraNode" Guid="{guid}" Name="Native camera">
        <Position>{matrix}<D X="{position}" Y="2" Z="3"/></Position>
        <Target>{matrix}<D X="0" Y="0.1" Z="0"/></Target>
        <CameraType>Perspective</CameraType><PerspectiveVFieldOfView>0.3</PerspectiveVFieldOfView>
        <OrthoHeight>5</OrthoHeight><AspectRatio X="1920" Y="1080"/>
        <ParentTransformGuid>{parent or bridge._ZERO_GUID}</ParentTransformGuid>
      </BaseNode>
      <BaseNode xsi:type="BackgroundNode" Guid="ground" Name="Studio">
        <EnableReflection>true</EnableReflection><EnableGroundShadow>false</EnableGroundShadow>
        <Brightness>1</Brightness><TextureNodeGuid>native-texture</TextureNodeGuid>
      </BaseNode><BaseNode xsi:type="ModelNode" Guid="geometry" Name="Untouched"/>
    </ProjectDocument>'''.encode()
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Project.xml", project)
        archive.writestr("Currents.xml", f"<ProjectCurrentsStorage><Camera>{guid}</Camera>"
                         "<Background>ground</Background></ProjectCurrentsStorage>")
        archive.writestr("Assets/native.bmd", b"native mesh bytes\0\xff")
        if extra:
            info = zipfile.ZipInfo("placeholder")
            info.filename = extra[0]
            archive.writestr(info, extra[1])
    return path


def test_copy_camera_preserves_source_assets_identity_and_namespaces(tmp_path):
    source = package(tmp_path / "source.svpj")
    reference = package(tmp_path / "reference.svpj", guid="reference-camera", position="4.5")
    original = source.read_bytes(), reference.read_bytes()
    output = tmp_path / "copy.svpj"
    result = bridge._prepare_project(source, output, reference, False, True)
    assert (source.read_bytes(), reference.read_bytes()) == original
    assert result["camera"]["source_guid"] == "camera-source"
    assert result["camera"]["reference_guid"] == "reference-camera"
    assert result["camera"]["readback"]["position_mm"] == [4500, 2000, 3000]
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        assert before.read("Assets/native.bmd") == after.read("Assets/native.bmd")
        assert before.read("Currents.xml") == after.read("Currents.xml")
        xml = after.read("Project.xml")
        assert b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' in xml
        assert b'xmlns:xsd="http://www.w3.org/2001/XMLSchema"' in xml
        root = ET.fromstring(xml)
        assert root.find("BaseNode/Position/D").get("X") == "4.5"
        ground = root.findall("BaseNode")[1]
        assert ground.findtext("EnableReflection") == "false"
        assert ground.findtext("EnableGroundShadow") == "true"
        assert ground.findtext("TextureNodeGuid") == "native-texture"


def test_camera_override_is_meter_scaled_orthonormal_and_read_back(tmp_path):
    source = package(tmp_path / "source.svpj")
    result = bridge._prepare_project(source, tmp_path / "out.svpj", None, False, True,
                                     [800, 550, 1600], [-70, 110, 0], 32)
    camera = result["camera"]["readback"]
    assert camera["position_mm"] == [800, 550, 1600]
    assert camera["target_mm"] == [-70, 110, 0]
    assert camera["vertical_fov_deg"] == pytest.approx(32)
    axes = list(camera["basis"].values())
    for i, a in enumerate(axes):
        for j, b in enumerate(axes):
            assert sum(x*y for x, y in zip(a, b)) == pytest.approx(int(i == j), abs=1e-12)


@pytest.mark.parametrize("position,target", [([0, 0, 0], [0, 0, 0]), ([0, 100, 0], [0, 0, 0])])
def test_rejects_degenerate_camera_direction(tmp_path, position, target):
    source = package(tmp_path / "source.svpj")
    with pytest.raises(bridge.ProjectRenderError, match="INVALID_CAMERA"):
        bridge._prepare_project(source, tmp_path / "out.svpj", None, False, True, position, target)


@pytest.mark.parametrize("name", ["../escape", "C:/escape", "Assets\\escape", "/escape", "project.XML"])
def test_archive_rejects_unsafe_or_case_duplicate_names(tmp_path, name):
    source = package(tmp_path / "source.svpj", extra=(name, b"x"))
    with pytest.raises(bridge.ProjectRenderError, match="INVALID_PROJECT_ARCHIVE"):
        bridge._prepare_project(source, tmp_path / "out.svpj", None, False, True)


def test_rejects_xml_entities_and_expansion_limit(tmp_path, monkeypatch):
    with pytest.raises(bridge.ProjectRenderError, match="INVALID_PROJECT_XML"):
        bridge._xml(b'<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>')
    source = package(tmp_path / "source.svpj")
    monkeypatch.setattr(bridge, "MAX_EXPANDED_BYTES", 10)
    with pytest.raises(bridge.ProjectRenderError, match="PROJECT_TOO_LARGE"):
        bridge._prepare_project(source, tmp_path / "out.svpj", None, False, True)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_encoded_xml_entities_are_also_rejected(encoding):
    raw = '<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>'.encode(encoding)
    with pytest.raises(bridge.ProjectRenderError, match="INVALID_PROJECT_XML"):
        bridge._xml(raw)


def test_rejects_nan_and_parented_reference_camera(tmp_path):
    source = package(tmp_path / "source.svpj")
    for name, args in (("nan", {"position": "nan"}), ("parent", {"parent": "other-model-guid"})):
        ref = package(tmp_path / f"{name}.svpj", **args)
        with pytest.raises(bridge.ProjectRenderError, match="INVALID_CAMERA|UNSUPPORTED_CAMERA_PARENT"):
            bridge._prepare_project(source, tmp_path / f"{name}-out.svpj", ref, False, True)


@pytest.fixture
def fake_native(tmp_path, monkeypatch):
    executable = tmp_path / "OfflineRenderer.exe"
    executable.write_bytes(b"fixture, never executed")
    monkeypatch.setattr(bridge, "RENDERER_PATH", executable)
    monkeypatch.setattr(bridge, "_running_renderers", lambda: [])
    calls = []
    def launch(argv, directory):
        calls.append(argv)
        folder = directory / "render"
        folder.mkdir()
        Image.new("RGB", (192, 128), "white").save(folder / "render.png")
        return {"pid": 100, "exit_code": 0}
    monkeypatch.setattr(bridge, "_launch", launch)
    return calls


def test_render_uses_fixed_argv_and_verifies_owned_nested_png(tmp_path, fake_native):
    source = package(tmp_path / "Türkçe project $literal.svpj")
    result = bridge.render(source, tmp_path / "output", width=192, height=128)
    assert result["ok"] is True
    assert result["renderer"]["processes_after"] == []
    assert fake_native[0][0] == str(bridge.RENDERER_PATH)
    assert fake_native[0][1:3] == ["-file", str(tmp_path / "output/scene.svpj")]
    assert fake_native[0][-1] == "-denoise"
    image = result["images"][0]
    assert image["sha256"] == hashlib.sha256(Path(image["path"]).read_bytes()).hexdigest()
    assert result["source_project"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["camera"]["readback"]["aspect_ratio"] == {"X": 192, "Y": 128}


def test_busy_renderer_rejected_before_output_creation(tmp_path, fake_native, monkeypatch):
    source = package(tmp_path / "source.svpj")
    monkeypatch.setattr(bridge, "_running_renderers", lambda: [{"pid": 99, "name": "OfflineRenderer.exe"}])
    with pytest.raises(bridge.ProjectRenderError, match="RENDERER_BUSY"):
        bridge.render(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert not fake_native


def test_existing_output_refused(tmp_path, fake_native):
    source = package(tmp_path / "source.svpj")
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(bridge.ProjectRenderError, match="INVALID_OUTPUT_PATH"):
        bridge.render(source, output)
    assert not fake_native


@pytest.mark.parametrize("kwargs", [{"width": True}, {"passes": 0}, {"denoiser": 1},
                                      {"camera_position_mm": [1, 2, 3]},
                                      {"camera_position_mm": [1, math.nan, 3]},
                                      {"vertical_fov_deg": 151}, {"vertical_fov_deg": True}])
def test_invalid_arguments_never_launch(tmp_path, fake_native, kwargs):
    with pytest.raises(bridge.ProjectRenderError):
        bridge.render(tmp_path / "missing.svpj", tmp_path / "output", **kwargs)
    assert not fake_native


def test_native_failure_and_wrong_dimensions_are_errors(tmp_path, fake_native, monkeypatch):
    source = package(tmp_path / "source.svpj")
    with pytest.raises(bridge.ProjectRenderError, match="INVALID_RENDER_OUTPUT"):
        bridge.render(source, tmp_path / "wrong", width=256, height=128)
    monkeypatch.setattr(bridge, "_launch", lambda argv, directory: {"pid": 100, "exit_code": 4})
    with pytest.raises(bridge.ProjectRenderError, match="NATIVE_RENDER_FAILED"):
        bridge.render(source, tmp_path / "failed")


def test_process_postflight_must_be_idle(tmp_path, fake_native, monkeypatch):
    source = package(tmp_path / "source.svpj")
    snapshots = iter([[], [], [{"pid": 101, "name": "OfflineRenderer.exe"}]])
    monkeypatch.setattr(bridge, "_running_renderers", lambda: next(snapshots))
    with pytest.raises(bridge.ProjectRenderError, match="RENDERER_STILL_ACTIVE"):
        bridge.render(source, tmp_path / "output", width=192, height=128)


def test_launcher_is_hidden_without_shell(tmp_path, monkeypatch):
    observed = {}
    class Process:
        pid = 123
        def __init__(self, argv, **kwargs):
            observed.update(argv=argv, **kwargs)
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(bridge.subprocess, "Popen", Process)
    result = bridge._launch([str(bridge.RENDERER_PATH), "-file", "one file.svpj"], tmp_path)
    assert result == {"pid": 123, "exit_code": 0}
    assert observed["shell"] is False
    assert observed["argv"][2] == "one file.svpj"
    assert observed["creationflags"] == getattr(bridge.subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize("first_result", [0, 1])
def test_process_enumeration_errors_fail_closed_and_release_handle(monkeypatch, first_result):
    closed = []
    class Function:
        def __init__(self, fn):
            self.fn = fn
        def __call__(self, *args):
            return self.fn(*args)
    kernel = SimpleNamespace(
        CreateToolhelp32Snapshot=Function(lambda *args: 10),
        Process32FirstW=Function(lambda *args: first_result),
        Process32NextW=Function(lambda *args: 0),
        CloseHandle=Function(lambda handle: closed.append(handle) or 1),
    )
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(bridge.ctypes, "get_last_error", lambda: 5, raising=False)
    with pytest.raises(bridge.ProjectRenderError, match="PROCESS_INSPECTION_FAILED"):
        bridge._running_renderers()
    assert closed == [10]
