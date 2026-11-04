"""Independent native document, export, drawing and viewport operations."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from uuid import uuid4

from . import binding, ENGINE_ID
from .errors import CadError
from .session import same_object, feature_walk


def _file(path, suffix=None, *, exists=False):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise CadError("INVALID_PATH", "A nonempty local file path is required.")
    target = Path(path).expanduser()
    if suffix and not target.suffix:
        target = target.with_suffix(suffix)
    if str(target).startswith(("\\\\", "//")) or target.is_symlink():
        raise CadError("INVALID_PATH", "Use a regular local file, not a network path or symbolic link.")
    target = target.resolve()
    if exists and not target.is_file():
        raise CadError("FILE_NOT_FOUND", f"The requested file does not exist: {target}")
    if not exists:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _evidence(path):
    if not path.is_file() or path.is_symlink() or not path.stat().st_size:
        raise CadError("NATIVE_FILE_OUTPUT_MISSING", f"Native output was not written: {path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest}


def _row(session, model):
    rights = session.rights(model)
    return {"title": str(model.GetTitle()), "path": str(model.GetPathName()), "type": int(model.GetType()),
            "dirty": bool(model.GetSaveFlag()), "editable": rights["editable"], "opened_by_session": rights["opened"]}


def _identity_evidence(model):
    """Read diagnostic identity without letting a stale proxy mask the cause."""
    if model is None:
        return {"available": False}
    evidence = {"available": True}
    for key, method in (("title", "GetTitle"), ("path", "GetPathName"), ("type", "GetType")):
        try:
            evidence[key] = getattr(model, method)()
        except Exception as exc:
            evidence[key+"_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    return evidence


def get_status(session):
    session._reattach_if_gone()
    if session._sw is None:
        return {"ok": True, "connected": False, "native_engine": ENGINE_ID, "active_document": None,
                "current_part": None, "note": "No running SOLIDWORKS connection; CAD is never launched by a status query."}
    active = binding.wrap(session._sw.ActiveDoc, "IModelDoc2")
    return {"ok": True, "connected": True, "native_engine": ENGINE_ID,
            "revision": str(session._sw.RevisionNumber()),
            "active_document": _row(session, active) if active is not None else None,
            "current_part": str(session._model.GetTitle()) if session._model is not None else None,
            "thread_id": session._owner}


def list_documents(session):
    if session.attach() is None:
        return {"ok": True, "documents": [], "count": 0, "connected": False}
    values = [_row(session, document) for document in session._open_documents()]
    return {"ok": True, "documents": values, "count": len(values)}


def _new(session, kind):
    app = session._ensure()
    preference = {1: "swDefaultTemplatePart", 2: "swDefaultTemplateAssembly", 3: "swDefaultTemplateDrawing"}[kind]
    template = str(app.GetUserPreferenceStringValue(binding.constant(preference)))
    if not template or not Path(template).is_file():
        raise CadError("DEFAULT_TEMPLATE_MISSING", f"Configure the native default {preference} template first.")
    paper = binding.constant("swDwgPaperA4size") if kind == 3 else 0
    previous_command = session._command_in_progress(True)
    try:
        model = binding.wrap(app.NewDocument(template, paper, 0.297 if kind == 3 else 0.0,
                                            0.21 if kind == 3 else 0.0), "IModelDoc2")
        if model is None or int(model.GetType()) != kind:
            raise CadError("DOCUMENT_CREATE_FAILED", "The native default template did not produce the requested document type.")
        if not same_object(binding.wrap(app.ActiveDoc, "IModelDoc2"), model):
            raise CadError("DOCUMENT_CREATE_UNVERIFIED", "The newly created document is not active.", outcome_may_be_indeterminate=True)
        session._model = model
        session.remember(model, editable=True, opened=True)
        session._topology_cache = {}
        return {"ok": True, "title": str(model.GetTitle()), "type": kind, "created": True, "saved": False,
                "template": template, "units": {"length": "mm", "angle": "degrees", "mass": "kg"}}
    finally:
        if previous_command is not None:
            session._command_in_progress(previous_command)


def new_part(session):
    return _new(session, 1)


def new_assembly(session):
    return _new(session, 2)


def activate_document(session, title, allow_edit=False):
    if not isinstance(title, str) or not title or type(allow_edit) is not bool:
        raise CadError("INVALID_ARGUMENT", "Use an exact document title and boolean allow_edit.")
    app = session._ensure()
    candidates = [model for model in session._open_documents() if str(model.GetTitle()) == title]
    if len(candidates) != 1:
        raise CadError("DOCUMENT_NOT_UNIQUE", f"Found {len(candidates)} open documents with that exact title.")
    returned = app.ActivateDoc3(title, False, 1, 0)
    model = binding.wrap(returned[0] if isinstance(returned, (tuple, list)) else returned, "IModelDoc2")
    if model is None or not same_object(model, candidates[0]) or not same_object(binding.wrap(app.ActiveDoc, "IModelDoc2"), model):
        raise CadError("DOCUMENT_ACTIVATION_FAILED", "Native activation did not select the exact requested document.")
    session._model = model
    session.remember(model, editable=allow_edit)
    session._topology_cache = {}
    return {"ok": True, "document": _row(session, model), "title": title}


def _open(session, path, kind, allow_large, allow_edit):
    if type(allow_large) is not bool or type(allow_edit) is not bool:
        raise CadError("INVALID_ARGUMENT", "allow_large and allow_edit must be booleans.")
    target = _file(path, exists=True)
    if target.stat().st_size > 30 * 1024 * 1024 and not allow_large:
        raise CadError("LARGE_FILE_OPT_IN_REQUIRED", "Files over 30 MiB require allow_large=true.")
    app = session._ensure()
    previous = session._open_documents()
    native = target.suffix.lower() in {".sldprt", ".sldasm"}
    if native:
        if target.suffix.lower() != {1: ".sldprt", 2: ".sldasm"}[kind]:
            raise CadError("DOCUMENT_TYPE_MISMATCH", "Use the matching part or assembly tool for this file.")
        response = app.OpenDoc6(str(target), kind, 1, "", 0, 0)
    else:
        if kind != 1 or target.suffix.lower() not in {".step", ".stp", ".iges", ".igs", ".x_t", ".x_b", ".sat", ".3mf"}:
            raise CadError("IMPORT_FORMAT_UNSUPPORTED", "This part import format is not supported.")
        import_data = app.GetImportFileData(str(target))
        response = app.LoadFile4(str(target), "", import_data, 0)
    raw = response[0] if isinstance(response, (tuple, list)) else response
    model = binding.wrap(raw, "IModelDoc2")
    if model is None or (isinstance(response, (tuple, list)) and len(response) > 1 and response[1]):
        raise CadError("DOCUMENT_OPEN_FAILED", "The native open/import API did not report a usable document.")
    newly_opened = not any(same_object(model, document) for document in previous)
    if int(model.GetType()) != kind:
        if newly_opened:
            session.close_document(model)
        raise CadError("DOCUMENT_TYPE_MISMATCH", "The opened/imported document has the wrong native type.")
    if native and Path(model.GetPathName()).resolve() != target:
        raise CadError("DOCUMENT_PATH_MISMATCH", "Native open returned a different document.")
    session._model = model
    session.remember(model, editable=allow_edit or not native, opened=newly_opened)
    session._topology_cache = {}
    result = {"ok": True, "title": str(model.GetTitle()), "document": _row(session, model),
              "imported": not native, "source_path": str(target)}
    if not native:
        result.update(session.get_mass_properties())
    return result


def open_part(session, path, allow_large=False, allow_edit=False):
    return _open(session, path, 1, allow_large, allow_edit)


def open_assembly(session, path, allow_large=False, allow_edit=False):
    return _open(session, path, 2, allow_large, allow_edit)


def _save_native(model, target, *, require_identity=True):
    returned = model.SaveAs3(str(target), 0, 1)
    if type(returned) not in (int, bool):
        raise CadError("NATIVE_SAVE_RESULT_INVALID", "The native save API did not return its required error code.")
    if isinstance(returned, bool) and not returned:
        raise CadError("NATIVE_SAVE_FAILED", "The native save API returned false.")
    if type(returned) is int and returned != 0:
        raise CadError("NATIVE_SAVE_FAILED", "The native save API reported an error.", native_code=returned)
    evidence = _evidence(target)
    if require_identity and (Path(model.GetPathName()).resolve() != target or bool(model.GetSaveFlag())):
        raise CadError("NATIVE_SAVE_UNVERIFIED", "Saved document identity or clean-state readback did not match.")
    return {"ok": True, "saved": True, **evidence}


def save_part(session, path):
    target = _file(path, ".sldprt")
    if target.suffix.lower() != ".sldprt":
        raise CadError("DOCUMENT_TYPE_MISMATCH", "Save a native part with the .sldprt extension.")
    return _save_native(session._require_part(), target)


def save_assembly(session, path):
    model = session._require_model()
    if int(model.GetType()) != 2:
        raise CadError("ASSEMBLY_REQUIRED", "Saving an assembly requires an assembly target.")
    target = _file(path, ".sldasm")
    if target.suffix.lower() != ".sldasm":
        raise CadError("DOCUMENT_TYPE_MISMATCH", "Save a native assembly with the .sldasm extension.")
    return _save_native(model, target)


def close_part(session, save=False):
    if type(save) is not bool:
        raise CadError("INVALID_ARGUMENT", "save must be boolean.")
    model = session._require_model()
    rights = session.rights(model)
    if not rights["opened"] and not rights["editable"]:
        raise CadError("DOCUMENT_NOT_AUTHORIZED", "This session did not open or adopt the target document.")
    if save:
        existing = str(model.GetPathName())
        if not existing:
            raise CadError("SAVE_PATH_REQUIRED", "Use save_part/save_assembly with an explicit new path first.")
        _save_native(model, Path(existing).resolve())
    closed = session.close_document(model)
    session._model = None
    session._topology_cache = {}
    return {"ok": True, **closed, "saved": save}


def rebuild(session, top_only=False):
    if type(top_only) is not bool:
        raise CadError("INVALID_ARGUMENT", "top_only must be boolean.")
    model = session._require_model()
    result = bool(model.ForceRebuild3(top_only))
    broken = []
    for feature in feature_walk(model):
        code, warning = feature.GetErrorCode2()
        if code and not warning:
            broken.append({"name": str(feature.Name), "native_code": int(code)})
    return {"ok": result and not broken, "rebuild_ok": result, "broken_features": broken}


def export(session, path, file_format=None, quality="fine", deviation_mm=None, angle_deg=None, per_component=False):
    model = session._require_model()
    aliases = {"step": ".step", "stl": ".stl", "iges": ".igs", "parasolid": ".x_t", "3mf": ".3mf"}
    if file_format is not None and (not isinstance(file_format, str) or file_format.lower() not in aliases):
        raise CadError("EXPORT_FORMAT_UNSUPPORTED", "file_format must name STEP, STL, IGES, Parasolid or 3MF.")
    suffix = aliases.get(str(file_format).lower()) if file_format else None
    target = _file(path, suffix)
    if target.suffix.lower() not in {".step", ".stp", ".stl", ".iges", ".igs", ".x_t", ".x_b", ".3mf"}:
        raise CadError("EXPORT_FORMAT_UNSUPPORTED", "Use STEP, STL, IGES, Parasolid or 3MF output.")
    if quality not in {"coarse", "fine"} or type(per_component) is not bool:
        raise CadError("INVALID_ARGUMENT", "quality must be coarse/fine and per_component must be boolean.")
    if suffix is not None:
        equivalent = {".step": {".step", ".stp"}, ".igs": {".igs", ".iges"},
                      ".x_t": {".x_t", ".x_b"}}.get(suffix, {suffix})
        if target.suffix.lower() not in equivalent:
            raise CadError("EXPORT_FORMAT_MISMATCH", "The output extension conflicts with the requested file_format.")
    if per_component and target.suffix.lower() != ".stl":
        raise CadError("COMPONENT_EXPORT_FORMAT_UNSUPPORTED", "Separate component files require STL output.")
    if deviation_mm is not None and (type(deviation_mm) not in (int, float) or not math.isfinite(deviation_mm) or deviation_mm <= 0):
        raise CadError("INVALID_ARGUMENT", "Custom tessellation deviation must be positive finite mm.")
    if angle_deg is not None and (type(angle_deg) not in (int, float) or not math.isfinite(angle_deg) or not 0 < angle_deg < 180):
        raise CadError("INVALID_ARGUMENT", "Custom tessellation angle must be between 0 and 180 degrees.")
    app, saved = session._ensure(), []
    preferences_started = False
    previous_files = set(target.parent.glob("*.stl")) if per_component else set()
    original_path = str(model.GetPathName())
    def preference(kind, name, value):
        getter, setter, key, _, _ = next(record for record in saved if record[4] == name)
        if getter(key) != value:
            setter(key, value)
        if getter(key) != value:
            raise CadError("EXPORT_PREFERENCE_REJECTED", f"Native export preference {name} was not retained.")
    result, failure = None, None
    try:
        if target.suffix.lower() in {".stl", ".3mf"}:
            # Preset quality and tolerance setters can change one another.
            # Capture the complete state before the first native write, even
            # for tolerances not explicitly supplied by this export request.
            settings = [("Toggle", "swSTLBinaryFormat"), ("Toggle", "swSTLDontTranslateToPositive"),
                ("IntegerValue", "swExportStlUnits"), ("Toggle", "swSTLComponentsIntoOneFile"),
                ("Toggle", "swSTLCheckForInterference"), ("Toggle", "swSTLPreview"),
                ("Toggle", "swSTLShowInfoOnSave"), ("DoubleValue", "swSTLDeviation"),
                ("DoubleValue", "swSTLAngleTolerance"), ("IntegerValue", "swSTLQuality")]
            snapshot = []
            for kind, name in settings:
                key = binding.constant(name)
                getter = getattr(app, "GetUserPreference" + kind)
                setter = getattr(app, "SetUserPreference" + kind)
                snapshot.append((getter, setter, key, getter(key), name))
            saved = snapshot
            preferences_started = True
            preference("Toggle", "swSTLBinaryFormat", True)
            preference("Toggle", "swSTLDontTranslateToPositive", True)
            preference("IntegerValue", "swExportStlUnits", binding.constant("swMM"))
            custom = deviation_mm is not None or angle_deg is not None
            preference("IntegerValue", "swSTLQuality", binding.constant(
                "swSTLQuality_Custom" if custom else "swSTLQuality_Fine" if quality == "fine" else "swSTLQuality_Coarse"))
            preference("Toggle", "swSTLComponentsIntoOneFile", not per_component)
            if custom:
                previous = {record[4]: record[3] for record in saved}
                preference("DoubleValue", "swSTLDeviation", deviation_mm / 1000 if deviation_mm is not None
                           else previous["swSTLDeviation"])
                preference("DoubleValue", "swSTLAngleTolerance", math.radians(angle_deg) if angle_deg is not None
                           else previous["swSTLAngleTolerance"])
                preference("IntegerValue", "swSTLQuality", binding.constant("swSTLQuality_Custom"))
        result = _save_native(model, target, require_identity=False)
        files = sorted(set(target.parent.glob("*.stl")) - previous_files) if per_component else [target]
        if not files:
            raise CadError("NATIVE_FILE_OUTPUT_MISSING", "Component export produced no new STL files.")
        if str(model.GetPathName()) != original_path:
            raise CadError("EXPORT_SOURCE_IDENTITY_CHANGED", "Native export unexpectedly changed the source document path.",
                           original_path=original_path, current_path=str(model.GetPathName()),
                           outcome_may_be_indeterminate=True)
        result.update(files=[_evidence(file) for file in files], saved_model=False,
                      frame="active_model", source_path_unchanged=True)
    except Exception as exc:
        failure = exc
    finally:
        restoration = []
        # The snapshot order restores tolerances before quality. Verify only
        # after all coupled writes, since a later native setter can invalidate
        # an earlier setting that initially read back correctly.
        for getter, setter, key, before, name in saved if preferences_started else ():
            try:
                if getter(key) != before:
                    setter(key, before)
            except Exception as exc:
                restoration.append(f"{name}: {exc}")
        for getter, setter, key, before, name in saved if preferences_started else ():
            try:
                if getter(key) != before:
                    raise RuntimeError(f"{name}: expected {before!r}, native readback {getter(key)!r}")
            except Exception as exc:
                restoration.append(str(exc))
        if restoration:
            raise CadError("EXPORT_SETTINGS_RESTORE_FAILED", "; ".join(restoration),
                           failure=str(failure) if failure else None, outcome_may_be_indeterminate=True)
    if failure:
        raise failure
    return result


def screenshot(session, path, view="iso", zoom_mm=None, show_planes=False, from_dir=None):
    from ..camera_bridge import _state, _restore, view_rotation, STANDARD_VIEWS, _math_object
    model, app = session._require_model(), session._ensure()
    target = _file(path, ".png")
    if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        raise CadError("IMAGE_FORMAT_UNSUPPORTED", "Use PNG, JPG, TIFF or BMP output.")
    if type(show_planes) is not bool or view not in {"iso", *STANDARD_VIEWS}:
        raise CadError("INVALID_ARGUMENT", "Use a standard view and boolean show_planes.")
    if zoom_mm is not None:
        if (not isinstance(zoom_mm, (tuple, list)) or len(zoom_mm) != 2
                or any(not isinstance(p, (tuple, list)) or len(p) != 3 for p in zoom_mm)
                or any(type(v) not in (int, float) or not math.isfinite(v) for p in zoom_mm for v in p)):
            raise CadError("INVALID_ARGUMENT", "zoom_mm requires two finite 3D corner points in mm.")
    snapshot = _state(model)
    flags, image_file, result, failure = [], target.parent / (uuid4().hex + ".bmp"), None, None
    try:
        for name in ("swDisplayPlanes", "swDisplayAxes"):
            key = binding.constant(name)
            before = bool(model.GetUserPreferenceToggle(key))
            flags.append((key, before))
            model.SetUserPreferenceToggle(key, show_planes)
        if from_dir is not None:
            orientation = view_rotation(from_dir)
            binding.wrap(model.ActiveView, "IModelView").Orientation3 = _math_object(app, "transform", [*orientation, 0, 0, 0, 1, 0, 0, 0])
        else:
            model.ShowNamedView2("", STANDARD_VIEWS["isometric" if view == "iso" else view])
        if zoom_mm is None:
            model.ViewZoomtofit2()
        else:
            model.ViewZoomTo2(*(float(v) / 1000 for p in zoom_mm for v in p))
        model.GraphicsRedraw2()
        native_view = binding.wrap(model.ActiveView, "IModelView")
        width = max(64, min(2048, int(native_view.FrameWidth)))
        height = max(64, min(2048, int(native_view.FrameHeight)))
        if not model.SaveBMP(str(image_file), width, height):
            raise CadError("SCREENSHOT_CAPTURE_FAILED", "Native viewport capture was refused.")
        from PIL import Image
        with Image.open(image_file) as image:
            image.load()
            image.convert("RGB").save(target)
            size = list(image.size)
        result = {"ok": True, **_evidence(target), "size_px": size, "kind": "native_viewport_not_photorealistic_render"}
    except Exception as exc:
        failure = exc
    finally:
        restored = _restore(app, model, snapshot)
        for key, value in reversed(flags):
            model.SetUserPreferenceToggle(key, value)
            if bool(model.GetUserPreferenceToggle(key)) != value:
                restored["verified"] = False
        if image_file.is_file() and image_file.parent == target.parent:
            image_file.unlink()
        if not restored.get("verified"):
            raise CadError("VIEW_RESTORATION_FAILED", "Viewport state restoration could not be verified.",
                           restoration=restored, outcome_may_be_indeterminate=True)
    if failure:
        raise failure
    return result


def make_drawing(session, path):
    source = session._require_part()
    source_path = str(source.GetPathName())
    source_title = str(source.GetTitle())
    if not source_path or not Path(source_path).is_file():
        raise CadError("SAVED_PART_REQUIRED", "Save the native part before creating a referenced drawing.")
    target = _file(path, ".slddrw")
    if target.suffix.lower() not in {".pdf", ".slddrw"}:
        raise CadError("DRAWING_FORMAT_UNSUPPORTED", "Use an editable .slddrw or a .pdf output.")
    app, drawing, result = session._ensure(), None, None
    try:
        _new(session, 3)
        drawing = session._model
        doc = binding.wrap(drawing, "IDrawingDoc")
        if not doc.Create1stAngleViews2(source_path):
            raise CadError("DRAWING_VIEWS_FAILED", "Native first-angle drawing views could not be created.")
        iso = doc.CreateDrawViewFromModelView3(source_path, "*Isometric", 0.22, 0.15, 0.0)
        if iso is None:
            raise CadError("DRAWING_ISOMETRIC_FAILED", "Native isometric drawing view was not created.")
        annotations = doc.InsertModelAnnotations3(0, 8, True, False, False, False) or ()
        result = _save_native(drawing, target, require_identity=target.suffix.lower() == ".slddrw")
        result.update(projection="first_angle", views=["front", "top", "right", "isometric"],
                      dimension_count=len(annotations) if isinstance(annotations, (tuple, list)) else None,
                      referenced_model=source_path)
    finally:
        if drawing is not None:
            session.close_document(drawing)
        returned = app.ActivateDoc3(source_title, False, 1, 0)
        restored = binding.wrap(returned[0] if isinstance(returned, (tuple, list)) else returned, "IModelDoc2")
        active = binding.wrap(app.ActiveDoc, "IModelDoc2")
        identities = {"returned": _identity_evidence(restored), "active": _identity_evidence(active),
                      "source": _identity_evidence(source), "requested_title": source_title,
                      "requested_path": source_path}
        checks = {"returned_available": restored is not None,
                  "source_path_matches": identities["returned"].get("path") == source_path}
        for key, first, second in (("active_matches_returned", active, restored),
                                   ("returned_matches_source", restored, source)):
            try:
                checks[key] = same_object(first, second)
            except Exception as exc:
                checks[key] = False
                identities[key+"_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        if not all(checks.values()):
            raise CadError("DOCUMENT_RESTORATION_FAILED", "Original drawing-source activation could not be verified.",
                           identity=identities, checks=checks, outcome_may_be_indeterminate=True)
        session._model = restored
    return result


TOOLS = {name: value for name, value in globals().copy().items() if name in {
    "get_status", "new_part", "new_assembly", "list_documents", "activate_document", "open_part", "open_assembly",
    "save_part", "save_assembly", "close_part", "rebuild", "export", "screenshot", "make_drawing"}}
