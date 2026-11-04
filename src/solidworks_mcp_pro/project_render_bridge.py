"""Render an owned SVPJ copy with the installed native Visualize renderer.

The active camera is copied from a reference package, avoiding the add-in's
visible-model auto-fit. No geometry/assets or source packages are modified.
The CLI switches are verified against the installed 2026 renderer metadata;
this is an installed executable integration, not a documented COM API.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import io
import math
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr
import zipfile


RENDERER_PATH = Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS Visualize\OfflineRenderer.exe")
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
RENDER_TIMEOUT_SECONDS = 1800
_LOCK = threading.Lock()
_XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"
CAMERA_FIELDS = (
    "Position", "Target", "MeshOffset", "ParentTransformGuid", "ParentTransformType",
    "AimParentTransformGuid", "CameraType", "PerspectiveVFieldOfView", "OrthoHeight",
    "AspectRatio", "AdditionalOffset", "ShiftCenter", "ShiftCrop", "ProjectionSkew",
    "ProjectionOffset", "ShiftSkew", "RegionOrigin", "RegionDimensions",
    "EnableRegionRendering", "RegionRectangleRelative", "AllowUnderGround",
    "EnableDepthOfField", "DepthOfFieldDistance", "EnableMotionBlur", "EnableFocus", "FocusPoint",
)


class ProjectRenderError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _project_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != ".svpj":
        raise ProjectRenderError("INVALID_PROJECT_PATH", f"{label} must be an absolute .svpj path.")
    if path.is_symlink() or not path.is_file():
        raise ProjectRenderError("INVALID_PROJECT_PATH", f"{label} must be an existing regular file.")
    path = path.resolve(strict=True)
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ProjectRenderError("PROJECT_TOO_LARGE", f"{label} exceeds the package byte limit.")
    return path


def _validate_archive(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > 10000:
        raise ProjectRenderError("INVALID_PROJECT_ARCHIVE", "Too many archive entries.")
    seen: set[str] = set()
    total = 0
    for info in entries:
        name = info.filename
        parts = PurePosixPath(name).parts
        normalized = name.rstrip("/").casefold()
        original_name = info.orig_filename
        if (not name or "\\" in original_name or ":" in name or "\x00" in original_name
                or name.startswith("/") or ".." in parts or normalized in seen
                or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
            raise ProjectRenderError("INVALID_PROJECT_ARCHIVE", "Unsafe, duplicate or encrypted entry.")
        seen.add(normalized)
        total += info.file_size
        if info.file_size > MAX_PACKAGE_BYTES or total > MAX_EXPANDED_BYTES:
            raise ProjectRenderError("PROJECT_TOO_LARGE", "Expanded package exceeds byte limits.")
    for name in ("Project.xml", "Currents.xml"):
        try:
            info = archive.getinfo(name)
        except KeyError as exc:
            raise ProjectRenderError("INVALID_PROJECT_ARCHIVE", f"Missing {name}.") from exc
        if info.file_size > MAX_XML_BYTES:
            raise ProjectRenderError("PROJECT_TOO_LARGE", f"{name} exceeds XML byte limit.")


def _xml(raw: bytes) -> ET.Element:
    # Markup is ASCII in UTF-8/16/32. Removing NUL code-unit padding makes
    # the declaration guard cover all encodings accepted by ElementTree.
    tokens = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in tokens or b"<!ENTITY" in tokens:
        raise ProjectRenderError("INVALID_PROJECT_XML", "DTD/entity declarations are not accepted.")
    try:
        return ET.fromstring(raw, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    except ET.ParseError as exc:
        raise ProjectRenderError("INVALID_PROJECT_XML", "Malformed project XML.") from exc


def _active(root: ET.Element, currents: ET.Element, kind: str) -> ET.Element:
    ids = currents.findall(kind)
    if len(ids) != 1 or not ids[0].text:
        raise ProjectRenderError("INVALID_PROJECT_XML", f"Exactly one active {kind} is required.")
    guid = ids[0].text.strip()
    matches = [n for n in root.iter("BaseNode") if n.get("Guid") == guid
               and n.get(_XSI, "").split(":")[-1] == f"{kind}Node"]
    if len(matches) != 1:
        raise ProjectRenderError("INVALID_PROJECT_XML", f"Active {kind} GUID is missing or ambiguous.")
    return matches[0]


def _finite(value: str | None, *, positive: bool = False) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise ProjectRenderError("INVALID_CAMERA", "Camera contains a nonnumeric value.") from exc
    if not math.isfinite(number) or abs(number) > 1e6 or (positive and number <= 0):
        raise ProjectRenderError("INVALID_CAMERA", "Camera values must be finite and bounded.")
    return number


def _validate_camera(camera: ET.Element) -> None:
    for name in ("Position", "Target"):
        matrix = camera.find(name)
        if matrix is None:
            raise ProjectRenderError("INVALID_CAMERA", f"Missing camera {name}.")
        for axis in "ABCD":
            vector = matrix.find(axis)
            if vector is None:
                raise ProjectRenderError("INVALID_CAMERA", f"Missing camera {name}/{axis}.")
            for coordinate in "XYZ":
                _finite(vector.get(coordinate))
    for name in ("ParentTransformGuid", "AimParentTransformGuid"):
        value = camera.findtext(name, _ZERO_GUID)
        if value != _ZERO_GUID:
            raise ProjectRenderError("UNSUPPORTED_CAMERA_PARENT", "Reference camera must be unparented.")
    if camera.findtext("CameraType") not in {"Perspective", "Orthographic"}:
        raise ProjectRenderError("INVALID_CAMERA", "Unsupported camera projection.")
    if _finite(camera.findtext("PerspectiveVFieldOfView"), positive=True) >= math.pi:
        raise ProjectRenderError("INVALID_CAMERA", "Camera field of view must be below pi radians.")
    _finite(camera.findtext("OrthoHeight"), positive=True)
    aspect = camera.find("AspectRatio")
    if aspect is None:
        raise ProjectRenderError("INVALID_CAMERA", "Missing camera aspect ratio.")
    _finite(aspect.get("X"), positive=True)
    _finite(aspect.get("Y"), positive=True)


def _vector(value: list[float] | None, label: str) -> list[float] | None:
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or abs(v) > 1e7 for v in value)):
        raise ProjectRenderError("INVALID_CAMERA", f"{label} must contain three finite bounded numbers.")
    return [float(v) for v in value]


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _camera_override(camera: ET.Element, position: list[float] | None,
                     target: list[float] | None, vertical_fov_deg: float | None) -> None:
    if position is not None and target is not None:
        direction = [(position[i]-target[i])/1000 for i in range(3)]
        length = math.sqrt(sum(v*v for v in direction))
        if length < 1e-6:
            raise ProjectRenderError("INVALID_CAMERA", "Camera position and target must be distinct.")
        forward = [v/length for v in direction]
        right = _cross([0., 1., 0.], forward)
        right_length = math.sqrt(sum(v*v for v in right))
        if right_length < 1e-4:
            raise ProjectRenderError("INVALID_CAMERA", "View direction is parallel to world-Y up.")
        right = [v/right_length for v in right]
        up = _cross(forward, right)
        matrix = camera.find("Position")
        target_matrix = camera.find("Target")
        assert matrix is not None and target_matrix is not None
        for axis, values in zip("ABCD", (right, up, forward, [v/1000 for v in position]), strict=True):
            node = matrix.find(axis)
            assert node is not None
            node.attrib.update({coordinate: format(value, ".17g")
                                for coordinate, value in zip("XYZ", values, strict=True)})
        node = target_matrix.find("D")
        assert node is not None
        node.attrib.update({coordinate: format(value/1000, ".17g")
                            for coordinate, value in zip("XYZ", target, strict=True)})
        for i, a in enumerate((right, up, forward)):
            for j, b in enumerate((right, up, forward)):
                if abs(sum(x*y for x, y in zip(a, b, strict=True)) - (1 if i == j else 0)) > 1e-9:
                    raise ProjectRenderError("INVALID_CAMERA", "Computed camera basis is not orthonormal.")
    if position is not None or vertical_fov_deg is not None:
        projection = camera.find("CameraType")
        assert projection is not None
        projection.text = "Perspective"
    if vertical_fov_deg is not None:
        fov = camera.find("PerspectiveVFieldOfView")
        assert fov is not None
        fov.text = format(math.radians(vertical_fov_deg), ".17g")


def _camera_summary(camera: ET.Element) -> dict:
    def vector(parent: str, axis: str) -> list[float]:
        node = camera.find(f"{parent}/{axis}")
        assert node is not None
        return [float(node.get(c, "nan")) for c in "XYZ"]
    return {"position_mm": [v*1000 for v in vector("Position", "D")],
            "target_mm": [v*1000 for v in vector("Target", "D")],
            "basis": {axis: vector("Position", axis) for axis in "ABC"},
            "projection": camera.findtext("CameraType"),
            "vertical_fov_deg": math.degrees(float(camera.findtext("PerspectiveVFieldOfView", "nan"))),
            "ortho_height_m": float(camera.findtext("OrthoHeight", "nan")),
            "aspect_ratio": {k: float(camera.find("AspectRatio").get(k, "nan")) for k in ("X", "Y")}}


def _serialize(root: ET.Element, original: bytes) -> bytes:
    # Preserve lexical QName prefixes used by xsi:type, including otherwise
    # unused namespace declarations. Native SVPJ declares xsd and xsi here.
    namespaces: dict[str, str] = {}
    for _, pair in ET.iterparse(io.BytesIO(original), events=("start-ns",)):
        prefix, uri = pair
        if prefix in namespaces and namespaces[prefix] != uri:
            raise ProjectRenderError("UNSUPPORTED_XML_NAMESPACE", "Rebound namespace prefixes are unsupported.")
        namespaces[prefix] = uri
    for prefix, uri in namespaces.items():
        try:
            ET.register_namespace(prefix, uri)
        except ValueError as exc:
            raise ProjectRenderError("UNSUPPORTED_XML_NAMESPACE", "Unsupported namespace prefix.") from exc
    result = ET.tostring(root, encoding="unicode", xml_declaration=False)
    end = result.index(">")
    additions = []
    for prefix, uri in namespaces.items():
        key = "xmlns" + (":" + prefix if prefix else "")
        if f"{key}=" not in result[:end]:
            additions.append(f" {key}={quoteattr(uri)}")
    result = result[:end] + "".join(additions) + result[end:]
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + result).encode("utf-8")


def _prepare_project(source: Path, target: Path, reference: Path | None,
                     ground_reflections: bool, ground_shadows: bool,
                     camera_position_mm: list[float] | None = None,
                     camera_target_mm: list[float] | None = None,
                     vertical_fov_deg: float | None = None,
                     width: int | None = None, height: int | None = None) -> dict:
    source_sha = _sha(source)
    reference_sha = _sha(reference) if reference else None
    try:
        with zipfile.ZipFile(source) as archive:
            _validate_archive(archive)
            original_xml = archive.read("Project.xml")
            root = _xml(original_xml)
            currents = _xml(archive.read("Currents.xml"))
            camera = _active(root, currents, "Camera")
            _validate_camera(camera)
            copied: list[str] = []
            reference_guid = None
            if reference:
                with zipfile.ZipFile(reference) as reference_archive:
                    _validate_archive(reference_archive)
                    reference_root = _xml(reference_archive.read("Project.xml"))
                    reference_currents = _xml(reference_archive.read("Currents.xml"))
                    reference_camera = _active(reference_root, reference_currents, "Camera")
                    _validate_camera(reference_camera)
                    reference_guid = reference_camera.get("Guid")
                    for field in CAMERA_FIELDS:
                        original = camera.findall(field)
                        incoming = reference_camera.findall(field)
                        if len(original) > 1 or len(incoming) > 1:
                            raise ProjectRenderError("INVALID_CAMERA", f"Ambiguous camera field {field}.")
                        if not incoming:
                            continue
                        index = list(camera).index(original[0]) if original else len(camera)
                        if original:
                            camera.remove(original[0])
                        camera.insert(index, copy.deepcopy(incoming[0]))
                        copied.append(field)
            _camera_override(camera, camera_position_mm, camera_target_mm, vertical_fov_deg)
            if width is not None and height is not None:
                aspect = camera.find("AspectRatio")
                assert aspect is not None
                aspect.attrib.update(X=str(width), Y=str(height))
            _validate_camera(camera)
            camera_expected = _camera_summary(camera)
            background = _active(root, currents, "Background")
            before = {}
            after = {"EnableReflection": ground_reflections, "EnableGroundShadow": ground_shadows}
            for field, value in after.items():
                nodes = background.findall(field)
                if len(nodes) != 1:
                    raise ProjectRenderError("INVALID_PROJECT_XML", f"Exactly one {field} is required.")
                before[field] = nodes[0].text
                nodes[0].text = str(value).lower()
            patched = _serialize(root, original_xml)
            # No extraction: asset contents and every other archive entry are
            # copied unchanged. Exclusive creation prevents overwrites.
            with zipfile.ZipFile(target, "x") as result:
                for info in archive.infolist():
                    if info.filename == "Project.xml":
                        result.writestr(info, patched)
                    else:
                        with archive.open(info) as reader, result.open(info, "w") as writer:
                            shutil.copyfileobj(reader, writer, 1024 * 1024)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        if isinstance(exc, ProjectRenderError):
            raise
        raise ProjectRenderError("INVALID_PROJECT_ARCHIVE", str(exc)) from exc
    if _sha(source) != source_sha or (reference and _sha(reference) != reference_sha):
        raise ProjectRenderError("SOURCE_CHANGED", "A source project changed while preparing the copy.")
    with zipfile.ZipFile(target) as verified:
        verified_root = _xml(verified.read("Project.xml"))
        verified_currents = _xml(verified.read("Currents.xml"))
        actual_camera = _camera_summary(_active(verified_root, verified_currents, "Camera"))
        actual_background = _active(verified_root, verified_currents, "Background")
        if (actual_camera != camera_expected or any(actual_background.findtext(k) != str(v).lower()
                                                   for k, v in after.items())):
            raise ProjectRenderError("PROJECT_POSTCONDITION_FAILED", "Patched camera/ground readback differs.")
    return {
        "source_project": {"path": str(source), "sha256": source_sha},
        "reference_project": {"path": str(reference), "sha256": reference_sha} if reference else None,
        "camera": {"source_guid": camera.get("Guid"), "reference_guid": reference_guid,
                   "copied_fields": copied, "readback": actual_camera, "readback_verified": True},
        "ground": {"before": before, "after": after},
    }


def _running_renderers() -> list[dict]:
    """Read the Windows process snapshot; no COM or shell subprocesses."""
    if sys.platform != "win32":
        raise ProjectRenderError("WINDOWS_REQUIRED", "Native Visualize rendering requires Windows.")
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise ProjectRenderError("PROCESS_INSPECTION_FAILED", "Cannot inspect renderer process state.")
    found = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.casefold() in {"offlinerenderer.exe", "offlinerendererconsole.exe"}:
                found.append({"pid": entry.th32ProcessID, "name": entry.szExeFile})
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ProjectRenderError("PROCESS_INSPECTION_FAILED", "Windows process enumeration did not complete.")
    finally:
        kernel.CloseHandle(snapshot)
    return found


def _launch(argv: list[str], directory: Path) -> dict:
    with (directory / "renderer.stdout.log").open("xb") as stdout, \
            (directory / "renderer.stderr.log").open("xb") as stderr:
        startup = None
        if hasattr(subprocess, "STARTUPINFO"):
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen(argv, cwd=str(RENDERER_PATH.parent), shell=False,
                                   stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), startupinfo=startup)
        try:
            code = process.wait(timeout=RENDER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            # Only the owned standalone renderer is stopped; never SOLIDWORKS
            # or another render process. The source CAD/SVPJ is not open here.
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            raise ProjectRenderError("RENDER_TIMEOUT", "Owned native renderer exceeded its time limit.") from exc
    return {"pid": process.pid, "exit_code": code}


def _verify_image(directory: Path, width: int, height: int) -> dict:
    from PIL import Image

    candidates = []
    for path in directory.rglob("*.png"):
        if len(candidates) >= 16:
            raise ProjectRenderError("INVALID_RENDER_OUTPUT", "Too many output images.")
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ProjectRenderError("INVALID_RENDER_OUTPUT", "Image escapes the owned output directory.")
        candidates.append(path)
    if len(candidates) != 1:
        raise ProjectRenderError("INVALID_RENDER_OUTPUT", "Expected exactly one native PNG in the owned directory.")
    path = candidates[0]
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ProjectRenderError("INVALID_RENDER_OUTPUT", "Native image dimensions/format do not match.")
            image.verify()
        with Image.open(path) as image:
            image.load()
    except (OSError, ValueError) as exc:
        raise ProjectRenderError("INVALID_RENDER_OUTPUT", "Native image cannot be decoded.") from exc
    return {"path": str(path), "width": width, "height": height, "format": "png",
            "mime_type": "image/png", "bytes": path.stat().st_size, "sha256": _sha(path)}


def render(project_path: str | Path, output_directory: str | Path, *,
           reference_project_path: str | Path | None = None,
           ground_reflections: bool = False, ground_shadows: bool = True,
           width: int = 1920, height: int = 1080, passes: int = 32,
           denoiser: bool = True, camera_position_mm: list[float] | None = None,
           camera_target_mm: list[float] | None = None,
           vertical_fov_deg: float | None = None) -> dict:
    """Copy/patch camera and ground, then render with the fixed installed EXE."""
    for name, value, lower, upper in (("width", width, 128, 8192), ("height", height, 128, 8192),
                                      ("passes", passes, 1, 10000)):
        if type(value) is not int or not lower <= value <= upper:
            raise ProjectRenderError("INVALID_ARGUMENT", f"{name} must be an integer in [{lower}, {upper}].")
    if width * height > 32 * 1024 * 1024:
        raise ProjectRenderError("INVALID_ARGUMENT", "Requested image exceeds the pixel limit.")
    if any(type(v) is not bool for v in (ground_reflections, ground_shadows, denoiser)):
        raise ProjectRenderError("INVALID_ARGUMENT", "Ground flags and denoiser must be boolean.")
    if ground_reflections and not ground_shadows:
        raise ProjectRenderError("INVALID_ARGUMENT", "Ground reflections require ground shadows.")
    position = _vector(camera_position_mm, "camera_position_mm")
    target = _vector(camera_target_mm, "camera_target_mm")
    if (position is None) != (target is None):
        raise ProjectRenderError("INVALID_CAMERA", "Camera position and target must be supplied together.")
    if vertical_fov_deg is not None and (isinstance(vertical_fov_deg, bool)
            or not isinstance(vertical_fov_deg, (int, float)) or not math.isfinite(vertical_fov_deg)
            or not 1 <= vertical_fov_deg <= 150):
        raise ProjectRenderError("INVALID_CAMERA", "vertical_fov_deg must be between 1 and 150 degrees.")
    source = _project_path(project_path, "project_path")
    reference = _project_path(reference_project_path, "reference_project_path") if reference_project_path else None
    output = Path(output_directory)
    if (not output.is_absolute() or output.exists() or output.is_symlink()
            or not output.parent.is_dir() or output.parent.is_symlink()):
        raise ProjectRenderError("INVALID_OUTPUT_PATH", "Output must be a new absolute directory under an existing parent.")
    output = output.parent.resolve(strict=True) / output.name
    if not RENDERER_PATH.is_file() or RENDERER_PATH.is_symlink():
        raise ProjectRenderError("RENDERER_NOT_INSTALLED", "The fixed native OfflineRenderer.exe was not found.")
    if not _LOCK.acquire(blocking=False):
        raise ProjectRenderError("RENDERER_BUSY", "Another project render is active in this process.")
    started = time.perf_counter()
    try:
        before = _running_renderers()
        if before:
            raise ProjectRenderError("RENDERER_BUSY", f"A native renderer is already running: {before}.")
        output.mkdir()
        copied_project = output / "scene.svpj"
        provenance = _prepare_project(source, copied_project, reference, ground_reflections, ground_shadows,
                                      position, target, vertical_fov_deg, width, height)
        prepared = time.perf_counter()
        # Check again after copying: never intentionally overlap an external
        # render that started during package preparation.
        if _running_renderers():
            raise ProjectRenderError("RENDERER_BUSY", "A native renderer started during preparation.")
        argv = [str(RENDERER_PATH), "-file", str(copied_project), "-out", str(output / "render.png"),
                "-res", str(width), str(height), "-frames", str(passes), "-renderer", "Accurate"]
        if denoiser:
            argv.append("-denoise")
        process = _launch(argv, output)
        rendered = time.perf_counter()
        after = _running_renderers()
        if after:
            raise ProjectRenderError("RENDERER_STILL_ACTIVE", f"Native renderer still active after process exit: {after}.")
        if process["exit_code"] != 0:
            raise ProjectRenderError("NATIVE_RENDER_FAILED", f"Renderer exited with {process['exit_code']}; see owned log files.")
        image = _verify_image(output, width, height)
        copied_project_sha = _sha(copied_project)
        completed = time.perf_counter()
        return {"ok": True, "project_path": str(copied_project), "project_sha256": copied_project_sha,
                **provenance, "images": [image],
                "renderer": {"path": str(RENDERER_PATH), **process, "processes_before": before,
                             "processes_after": after, "argv": argv, "native_execution": True},
                "timings_ms": {"prepare": round((prepared-started)*1000, 3),
                               "native_render": round((rendered-prepared)*1000, 3),
                               "verify_output": round((completed-rendered)*1000, 3),
                               "total": round((completed-started)*1000, 3)},
                "timing_scope": "Monotonic bridge wall time; includes native process startup and output. Not GPU kernel time."}
    finally:
        _LOCK.release()
