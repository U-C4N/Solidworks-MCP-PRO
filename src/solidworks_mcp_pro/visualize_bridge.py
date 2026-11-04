"""SOLIDWORKS Visualize Add-in API (2025+), invoked only on the CAD STA worker.

This is the actual photorealistic renderer, not a mesh viewer or screenshot.
The API renders the active SOLIDWORKS document using its appearances/scene.
Do not cancel/kill the worker while Render() is running. See docs/research.md.
"""

from __future__ import annotations

import hashlib
import ntpath
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


PROG_ID = "SolidWorks.Visualize.Implementation.VisualizeAddin"
TYPELIB_GUID = "{2615126C-0ACD-4C09-A4ED-A31DC6A93B14}"
ADDIN_CLSID = "{8FC35696-5C94-4093-AB24-77641F0F507F}"
ADDIN_FILENAME = "SolidWorks.Visualize.Implementation.DLL"
IMAGE_FORMATS = {"jpeg": 0, "png": 3}
GROUPING = {"appearance": 0, "part": 1, "none": 2}
LOAD_CODES = {
    -1: "unknown error", 0: "loaded", 1: "not loaded", 2: "already loaded",
    3: "file not found", 4: "add-ins disabled", 5: "load conflict",
    6: "registration error", 7: "license error",
}


class VisualizeError(RuntimeError):
    """A failure with a stable, agent-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _client_apartment() -> dict:
    """Record the actual caller apartment; remote add-in threads may differ."""
    import ctypes
    from ctypes import wintypes

    apartment = ctypes.c_int()
    qualifier = ctypes.c_int()
    ole = ctypes.OleDLL("ole32")
    probe = ole.CoGetApartmentType
    probe.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    probe.restype = wintypes.HRESULT if hasattr(wintypes, "HRESULT") else ctypes.c_long
    result = probe(ctypes.byref(apartment), ctypes.byref(qualifier))
    if result != 0 or apartment.value not in (0, 3):
        raise VisualizeError("COM_APARTMENT_INVALID", f"Expected STA client; HRESULT={result}, apartment={apartment.value}.")
    return {"type": "STA" if apartment.value == 0 else "MAINSTA", "qualifier": qualifier.value}


def _typed(obj: Any, interface: str) -> Any:
    """Use the installed type library, including explicit interface selection.

    Test doubles do not have QueryInterface. COM objects never leave the STA.
    The registered typelib is 1.0 in the inspected Visualize 2026 installation.
    """
    if obj is None:
        raise VisualizeError("VISUALIZE_UNAVAILABLE", "The Visualize add-in returned no object.")
    # comtypes returns an already typed vtable pointer, including typed retval
    # interfaces. Never feed this pointer into pywin32's IDispatch adapter.
    if hasattr(type(obj), "_iid_"):
        return obj
    raw = getattr(obj, "_oleobj_", obj)
    if not hasattr(raw, "QueryInterface"):
        return obj
    import pythoncom
    from win32com.client import gencache

    module = gencache.EnsureModule(TYPELIB_GUID, 0, 1, 0)
    if module is None or not hasattr(module, interface):
        raise VisualizeError("VISUALIZE_API_UNAVAILABLE", f"Installed type library lacks {interface}.")
    cls = getattr(module, interface)
    dispatch = raw.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch)
    return cls(dispatch)


def _vtable_manager(sw_app: Any, version2: bool) -> Any:
    """Use native typed vtable calls for Visualize's interface return values.

    SW2026 SP4.1 accepts QI(IVisualizeAddin), but IDispatch.Invoke for its
    GetAddinManager retval fails E_NOINTERFACE. The same getter works through
    the interface vtable in both .NET and comtypes. This is not a fallback that
    hides a failed call: all production Visualize calls use the verified path.
    """
    import comtypes.client

    module = comtypes.client.GetModule((TYPELIB_GUID, 1, 0))
    # Reacquire the existing ROT object on this SAME STA; never CreateObject.
    app = comtypes.client.GetActiveObject("SldWorks.Application", dynamic=True)
    active_pid, session_pid = app.GetProcessID, sw_app.GetProcessID
    active_pid = active_pid() if callable(active_pid) else active_pid
    session_pid = session_pid() if callable(session_pid) else session_pid
    if int(active_pid) != int(session_pid):
        raise VisualizeError("SOLIDWORKS_INSTANCE_MISMATCH", "Visualize ROT connection differs from the CAD session; refusing to target another instance.")
    obj = app.GetAddInObject(PROG_ID)
    if obj is None:
        raise VisualizeError("VISUALIZE_UNAVAILABLE", "The Visualize add-in returned no object.")
    raw = getattr(obj, "_comobj", obj)
    interface = module.IVisualizeAddin2 if version2 else module.IVisualizeAddin
    addin = raw.QueryInterface(interface)
    manager = addin.GetAddinManager2() if version2 else addin.GetAddinManager()
    if not manager:
        raise VisualizeError("VISUALIZE_UNAVAILABLE", "Visualize returned no manager.")
    return manager


def _revision(sw_app: Any) -> tuple[str, int]:
    rev = sw_app.RevisionNumber
    rev = str(rev() if callable(rev) else rev)
    try:
        major = int(rev.split(".")[0])
    except ValueError as exc:
        raise VisualizeError("SOLIDWORKS_VERSION_UNKNOWN", f"Unexpected revision {rev!r}.") from exc
    if major < 33:
        raise VisualizeError("VISUALIZE_VERSION_UNSUPPORTED", "Visualize Add-in API requires SOLIDWORKS 2025 or newer.")
    return rev, major


def _manager(sw_app: Any, *, version2: bool = False) -> Any:
    _, major = _revision(sw_app)
    if version2 and major < 34:
        raise VisualizeError("VISUALIZE_VERSION_UNSUPPORTED", "Part grouping export requires SOLIDWORKS 2026 or newer.")
    try:
        if getattr(sw_app, "_oleobj_", None) is not None:
            return _vtable_manager(sw_app, version2)
        obj = sw_app.GetAddInObject(PROG_ID)
        addin = _typed(obj, "IVisualizeAddin2" if version2 else "IVisualizeAddin")
        getter = addin.GetAddinManager2 if version2 else addin.GetAddinManager
        return _typed(getter(), "IVisualizeAddinManager2" if version2 else "IVisualizeAddinManager")
    except VisualizeError:
        raise
    except Exception as exc:
        raise VisualizeError(
            "VISUALIZE_UNAVAILABLE",
            "Enable SOLIDWORKS Visualize in SOLIDWORKS > Tools > Add-ins and check its license. "
            f"API connection failed: {exc}",
        ) from exc


def status(sw_app: Any) -> dict:
    """Probe loaded add-in/API; this does not start a render or enable add-ins."""
    try:
        revision, major = _revision(sw_app)
        manager = _manager(sw_app)
        _typed(manager.RenderOptions, "IRenderOptions")
        return {
            "ok": True,
            "available": True,
            "renderer": "SOLIDWORKS Visualize",
            "solidworks_revision": revision,
            "render_api": "IVisualizeAddinManager.Render",
            "project_grouping_supported": major >= 34,
            "license_verified": False,
            "note": "Add-in is reachable; a successful render is required to verify renderer/license readiness.",
        }
    except Exception as exc:
        return {"ok": False, "available": False, "error": str(exc), "code": getattr(exc, "code", "VISUALIZE_PROBE_FAILED")}


def _codebase_path(codebase: str) -> str:
    """Decode only a local absolute Windows file URI from Visualize registration."""
    if not isinstance(codebase, str):
        raise VisualizeError("VISUALIZE_REGISTRATION_INVALID", "Visualize CodeBase must be a file URI.")
    try:
        uri = urlsplit(codebase)
        decoded = unquote(uri.path, errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise VisualizeError("VISUALIZE_REGISTRATION_INVALID", "Invalid Visualize CodeBase URI.") from exc
    if uri.scheme.lower() != "file" or uri.netloc or uri.query or uri.fragment or "\x00" in decoded:
        raise VisualizeError("VISUALIZE_REGISTRATION_INVALID", "Visualize CodeBase must be a local file URI without a host/query/fragment.")
    if re.match(r"^/[A-Za-z]:[/\\]", decoded):
        decoded = decoded[1:]
    drive, tail = ntpath.splitdrive(decoded)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("/", "\\")):
        raise VisualizeError("VISUALIZE_REGISTRATION_INVALID", "Visualize CodeBase must use an absolute local Windows drive; UNC paths are refused.")
    normalized = ntpath.normpath(decoded)
    if ntpath.basename(normalized).lower() != ADDIN_FILENAME.lower():
        raise VisualizeError("VISUALIZE_REGISTRATION_INVALID", "Registered file is not the known SOLIDWORKS Visualize add-in DLL.")
    return normalized


def _installed_addin_path() -> Path:
    """Discover the fixed Visualize add-in, never a caller-provided DLL."""
    import winreg

    try:
        key_name = rf"CLSID\{ADDIN_CLSID}\InprocServer32"
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_name, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            codebase, value_type = winreg.QueryValueEx(key, "CodeBase")
        if value_type != winreg.REG_SZ:
            raise VisualizeError("VISUALIZE_REGISTRATION_INVALID", "Visualize CodeBase is not a registry string.")
        candidate = Path(_codebase_path(codebase))
        resolved = candidate.resolve(strict=True)
        if (not resolved.is_file() or resolved.name.lower() != ADDIN_FILENAME.lower()
                or not re.fullmatch(r"[A-Za-z]:", resolved.drive)):
            raise VisualizeError("VISUALIZE_REGISTRATION_INVALID", "Registered Visualize add-in does not resolve to a local DLL file.")
        return resolved
    except VisualizeError:
        raise
    except (OSError, ValueError) as exc:
        raise VisualizeError("VISUALIZE_NOT_INSTALLED", f"Cannot find the registered Visualize add-in: {exc}") from exc


def enable(sw_app: Any) -> dict:
    """Explicitly load only the registered Visualize add-in, then probe its API.

    Does not change startup settings, accept a DLL argument, or start rendering.
    The caller must opt into this action and hold the CAD worker lease.
    """
    current = status(sw_app)
    if current.get("available"):
        return {**current, "load_code": None, "already_loaded": True}
    try:
        _revision(sw_app)
        path = _installed_addin_path()
        code = int(sw_app.LoadAddIn(str(path)))
        if code not in (0, 2):
            return {"ok": False, "available": False, "code": "VISUALIZE_LOAD_FAILED", "load_code": code,
                    "error": f"Visualize add-in load failed: {LOAD_CODES.get(code, 'unrecognized load status')} ({code})."}
        return {**status(sw_app), "load_code": code, "already_loaded": code == 2, "addin_path": str(path)}
    except Exception as exc:
        return {"ok": False, "available": False, "code": getattr(exc, "code", "VISUALIZE_LOAD_FAILED"),
                "error": str(exc)}


def _active_document(sw_app: Any) -> None:
    if sw_app.ActiveDoc is None:
        raise VisualizeError("NO_ACTIVE_DOCUMENT", "Activate a SOLIDWORKS part or assembly before rendering/exporting.")


def _integer(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VisualizeError("INVALID_ARGUMENT", f"{name} must be an integer in [{minimum}, {maximum}].")


def _verify_images(folder: Path, image_format: str, width: int, height: int) -> list[dict]:
    from PIL import Image

    suffixes = {"png": {".png"}, "jpeg": {".jpg", ".jpeg"}}[image_format]
    images = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        # The renderer is local, but do not follow a filesystem link outside this job.
        if not path.resolve().is_relative_to(folder.resolve()):
            raise VisualizeError("RENDER_OUTPUT_INVALID", "An output file resolves outside the job directory.")
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                size, actual_format = image.size, image.format
            if size != (width, height) or actual_format != image_format.upper():
                raise ValueError(f"Expected {width}x{height} {image_format}, got {size} {actual_format}.")
        except Exception as exc:
            raise VisualizeError("RENDER_OUTPUT_INVALID", f"Cannot verify {path.name}: {exc}") from exc
        images.append({"path": str(path), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "width": width, "height": height, "mime_type": f"image/{image_format}"})
    if not images:
        raise VisualizeError("RENDER_OUTPUT_MISSING", "Visualize returned without a decodable output image; no success is claimed.")
    return images


def render(
    sw_app: Any,
    output_dir: Path,
    *,
    width: int = 1280,
    height: int = 720,
    passes: int = 250,
    denoiser: bool = False,
    image_format: str = "png",
    job_name: str = "render",
) -> dict:
    """Synchronously render active native CAD into a new/empty isolated folder.

    The caller owns the cross-process CAD lease and must hold it until this
    function finishes even after client cancellation. Camera/scene/appearances
    come from the active model. QualityLevel termination uses FrameCount.
    """
    _integer("width", width, 64, 8192)
    _integer("height", height, 64, 8192)
    _integer("passes", passes, 1, 10000)
    if not isinstance(denoiser, bool):
        raise VisualizeError("INVALID_ARGUMENT", "denoiser must be a boolean.")
    if image_format not in IMAGE_FORMATS:
        raise VisualizeError("INVALID_ARGUMENT", "image_format must be png or jpeg.")
    if not isinstance(job_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", job_name):
        raise VisualizeError("INVALID_ARGUMENT", "job_name must be 1-64 ASCII letters/digits/underscore/hyphen, beginning with a letter/digit.")
    if job_name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        raise VisualizeError("INVALID_ARGUMENT", "job_name is reserved by Windows.")
    folder = Path(output_dir)
    if not folder.is_absolute():
        raise VisualizeError("INVALID_ARGUMENT", "output_dir must be absolute.")
    if folder.is_symlink() or (folder.exists() and (not folder.is_dir() or any(folder.iterdir()))):
        raise VisualizeError("OUTPUT_EXISTS", "Rendering requires a new or empty non-linked job directory.")
    _active_document(sw_app)
    started = time.perf_counter()
    timings = {}
    process_guards = {}
    if getattr(sw_app, "_oleobj_", None) is not None:
        from .renderer_process import wait_for_renderer_idle
        process_guards["before_render"] = wait_for_renderer_idle()
        if not process_guards["before_render"]["ok"]:
            raise VisualizeError(process_guards["before_render"]["code"], str(process_guards["before_render"]))
        timings["renderer_idle_wait"] = process_guards["before_render"]["elapsed_ms"]
    manager = _manager(sw_app)
    options = _typed(manager.RenderOptions, "IRenderOptions")
    changes = {
        "ImageFormat": IMAGE_FORMATS[image_format], "FrameCount": passes,
        "Width": width, "Height": height, "JobName": job_name,
        "OutputFolder": str(folder), "DenoiserEnabled": denoiser,
        "TerminationMode": 1, "IncludeAlpha": False,
    }
    # Read all prior values before changing anything. Unsupported APIs fail here.
    before = {key: getattr(options, key) for key in changes}
    folder.mkdir(parents=True, exist_ok=True)
    result = None
    error = None
    restore_failures = []
    try:
        for key, value in changes.items():
            setattr(options, key, value)
            if getattr(options, key) != value:
                raise VisualizeError("RENDER_OPTION_REJECTED", f"Visualize did not retain {key}={value!r}.")
        render_started = time.perf_counter()
        timings["setup"] = round((render_started - started) * 1000, 3)
        execution = "direct_api"
        macro_evidence = None
        if getattr(sw_app, "_oleobj_", None) is not None:
            # The add-in's managed COM object can execute an external call on
            # its MTA RPC thread even though our client worker is STA. Native
            # P2M conversion uses WPF/OLE and needs SOLIDWORKS' in-process STA.
            from .visualize_macro import render_on_ui
            client_apartment = _client_apartment()
            macro_evidence = render_on_ui(sw_app, folder)
            macro_evidence["client_apartment"] = client_apartment
            accepted = macro_evidence["accepted"]
            execution = "solidworks_in_process_macro"
        else:
            accepted = manager.Render()
        verify_started = time.perf_counter()
        timings["native_render"] = round((verify_started - render_started) * 1000, 3)
        if not accepted:
            raise VisualizeError("RENDER_FAILED", "Visualize Render returned false. Check renderer/license/dialogs; partial files may remain.")
        images = _verify_images(folder, image_format, width, height)
        timings["verify_output"] = round((time.perf_counter() - verify_started) * 1000, 3)
        if getattr(sw_app, "_oleobj_", None) is not None:
            process_guards["after_render"] = wait_for_renderer_idle()
            timings["renderer_cleanup_wait"] = process_guards["after_render"]["elapsed_ms"]
            if not process_guards["after_render"]["ok"]:
                raise VisualizeError(process_guards["after_render"]["code"], str(process_guards["after_render"]))
        result = {"ok": True, "renderer": "SOLIDWORKS Visualize", "api": "IVisualizeAddinManager.Render",
                  "output_dir": str(folder), "images": images, "passes": passes, "denoiser": denoiser,
                  "execution": execution, "macro_evidence": macro_evidence, "renderer_process_guards": process_guards,
                  "verification": "Decoded image format and dimensions, file size and SHA-256; scene fidelity needs visual review."}
    except Exception as exc:
        error = exc
    finally:
        restore_started = time.perf_counter()
        for key, value in reversed(list(before.items())):
            try:
                setattr(options, key, value)
                if getattr(options, key) != value:
                    raise RuntimeError("read-back differs")
            except Exception as exc:
                restore_failures.append(f"{key}: {exc}")
        timings["restore_options"] = round((time.perf_counter() - restore_started) * 1000, 3)
    if restore_failures:
        raise VisualizeError("RENDER_SETTINGS_RESTORE_FAILED",
                             f"Some Visualize settings were not restored: {restore_failures}. "
                             f"Render error: {error}. Output directory: {folder}.")
    if error is not None:
        raise error
    timings["bridge_total"] = round((time.perf_counter() - started) * 1000, 3)
    result["timings_ms"] = timings
    result["timing_note"] = "native_render is wall time for native render dispatch, including fixed-macro overhead when used and any native import/setup; it is not measured GPU kernel time."
    return result


def open_project(sw_app: Any, path: str) -> dict:
    """Ask the installed 2026+ add-in to launch an existing Visualize project.

    LaunchVisualize is a void/HRESULT method, not a document-loaded callback.
    Successful return proves only API acceptance, even for an existing app.
    No active CAD document is needed and none is created, changed or closed.
    """
    from .visualizelaunch import _project_file

    project = _project_file(path)
    manager = _manager(sw_app, version2=True)
    accepted = manager.LaunchVisualize(str(project))
    if accepted is not None and (type(accepted) is not int or accepted != 0):
        raise VisualizeError("VISUALIZE_LAUNCH_UNEXPECTED_RESULT", "LaunchVisualize returned an unexpected value; project loading is unverified.")
    return {"ok": True, "status": "launch_requested", "project_path": str(project),
            "api": "IVisualizeAddinManager2.LaunchVisualize", "project_loaded": None,
            "verification": "The native Visualize API returned without an HRESULT error. Project loading is not independently verified."}


def export_project(sw_app: Any, path: Path, grouping: str = "appearance") -> dict:
    """Export active model to native SVPJ; preserve requested hierarchy on 2026+."""
    target = Path(path)
    if not target.is_absolute() or target.suffix.lower() != ".svpj":
        raise VisualizeError("INVALID_ARGUMENT", "path must be an absolute .svpj path.")
    if grouping not in GROUPING:
        raise VisualizeError("INVALID_ARGUMENT", "grouping must be appearance, part, or none.")
    if target.exists():
        raise VisualizeError("OUTPUT_EXISTS", "Refusing to overwrite an existing Visualize project.")
    _active_document(sw_app)
    _, major = _revision(sw_app)
    target.parent.mkdir(parents=True, exist_ok=True)
    execution = "direct_api"
    macro_evidence = None
    if major >= 34:
        if getattr(sw_app, "_oleobj_", None) is not None:
            from uuid import uuid4
            from .visualize_macro import export_on_ui

            client_apartment = _client_apartment()
            scratch = target.parent / ("pro_visualize_export_" + uuid4().hex)
            scratch.mkdir(exist_ok=False)
            macro_evidence = export_on_ui(sw_app, target, GROUPING[grouping], scratch)
            if macro_evidence.get("accepted") is not True:
                raise VisualizeError("PROJECT_EXPORT_NOT_ACCEPTED", "The fixed export macro did not report verified acceptance.")
            macro_evidence["client_apartment"] = client_apartment
            execution = "solidworks_in_process_macro"
        else:
            _manager(sw_app, version2=True).SaveWithPartGrouping(str(target), GROUPING[grouping])
        api = "IVisualizeAddinManager2.SaveWithPartGrouping"
    elif grouping == "appearance":
        _manager(sw_app).Save(str(target))
        api = "IVisualizeAddinManager.Save"
    else:
        raise VisualizeError("VISUALIZE_VERSION_UNSUPPORTED", "Explicit part/no grouping export requires SOLIDWORKS 2026+. Use appearance on 2025.")
    if not target.is_file() or target.stat().st_size == 0:
        raise VisualizeError("PROJECT_OUTPUT_MISSING", "Visualize did not create a non-empty SVPJ file.")
    return {"ok": True, "path": str(target), "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "grouping": grouping if major >= 34 else "legacy_default", "requested_grouping": grouping, "api": api,
            "execution": execution, "macro_evidence": macro_evidence,
            "verification": "Non-empty exported file and SHA-256; reopen in Visualize for scene acceptance."}
