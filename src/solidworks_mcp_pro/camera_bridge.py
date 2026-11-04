"""Control the active SOLIDWORKS model view on the owning CAD STA.

The Visualize add-in consumes the active document's view; this does not create
an ICamera feature, change geometry/materials, save a document or start a render.
The caller holds the CAD lease. A subsequent render must verify composition.

Documented APIs: IModelView.Orientation3, Translation3, Scale2, HasPerspective,
AddPerspective, RemovePerspective and ObjectSizesAway; IModelDoc2.ShowNamedView2
and ViewZoomtofit2. Installed SW2026 interop signatures were inspected without
connecting to SOLIDWORKS. Direction matrices use screen axes as columns.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any


STANDARD_VIEWS = {"front": 1, "back": 2, "left": 3, "right": 4, "top": 5,
                  "bottom": 6, "isometric": 7, "trimetric": 8, "dimetric": 9}
VIEWS = {"current", "custom", *STANDARD_VIEWS}


class CameraError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _typed(obj: Any, interface: str) -> Any:
    if obj is None or not hasattr(obj, "_oleobj_"):
        return obj
    from .native import binding

    return binding.wrap(obj, getattr(binding.module(), interface))


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise CameraError("INVALID_ARGUMENT", f"{label} must be a finite number in [{minimum}, {maximum}].")
    return float(value)


def _vector(value: Any, label: str) -> tuple[float, float, float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(type(number) not in (float, int) or not math.isfinite(number) for number in value)):
        raise CameraError("INVALID_ARGUMENT", f"{label} must contain three finite numbers.")
    length = math.hypot(*value)
    if not math.isfinite(length) or length == 0:
        raise CameraError("INVALID_ARGUMENT", f"{label} must have a finite, nonzero length.")
    return tuple(number / length for number in value)


def view_rotation(from_direction: Any, up_direction: Any = None) -> tuple[float, ...]:
    """MODEL→CAMERA direction; supplied up is projected onto the view plane.

    Columns are screen x/right, screen y/up, and screen z/toward the viewer,
    expressed in model coordinates. This convention matches Orientation3.
    """
    z = _vector(from_direction, "from_direction")
    up = _vector([0.0, 1.0, 0.0] if up_direction is None else up_direction, "up_direction")
    along = sum(a * b for a, b in zip(up, z))
    y = tuple(a - along * b for a, b in zip(up, z))
    length = math.hypot(*y)
    if length < 1e-6:
        raise CameraError("INVALID_ARGUMENT", "up_direction must not be parallel or nearly parallel to from_direction.")
    y = tuple(number / length for number in y)
    x = (y[1] * z[2] - y[2] * z[1], y[2] * z[0] - y[0] * z[2], y[0] * z[1] - y[1] * z[0])
    return tuple(axis[row] for row in range(3) for axis in (x, y, z))


def _document_file(value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise CameraError("INVALID_ARGUMENT", "document_path must be an absolute existing part/assembly path.")
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() not in {".sldprt", ".sldasm"} or not path.is_file() or path.stat().st_size == 0):
        raise CameraError("INVALID_ARGUMENT", "document_path must be an absolute local nonempty .sldprt or .sldasm file.")
    return path


def _same_path(left: Any, right: Path) -> bool:
    return isinstance(left, str) and os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _array(value: Any, length: int, label: str) -> list[float]:
    if (not isinstance(value, (list, tuple)) or len(value) != length
            or any(type(number) not in (int, float) or not math.isfinite(number) for number in value)):
        raise RuntimeError(f"Native {label} is not a finite {length}-element array.")
    return [float(number) for number in value]


def _view(model: Any) -> Any:
    view = _typed(model.ActiveView, "IModelView")
    if view is None:
        raise CameraError("NO_ACTIVE_VIEW", "The document has no active model view.")
    return view


def _state(model: Any) -> dict:
    view = _view(model)
    orientation = _typed(view.Orientation3, "IMathTransform")
    translation = _typed(view.Translation3, "IMathVector")
    perspective = bool(view.HasPerspective())
    scale = float(view.Scale2)
    if not math.isfinite(scale) or scale <= 0:
        raise RuntimeError("Native view scale must be finite and positive.")
    away = float(view.ObjectSizesAway) if perspective else None
    if perspective and (not math.isfinite(away) or away <= 0):
        raise RuntimeError("Native perspective distance ratio is invalid.")
    return {"orientation": _array(orientation.ArrayData, 16, "orientation"),
            "translation": _array(translation.ArrayData, 3, "translation"),
            "scale": scale, "perspective": perspective, "object_sizes_away": away,
            "named_camera_active": view.Camera is not None}


def _math_object(sw_app: Any, kind: str, values: list | tuple) -> Any:
    utility = _typed(sw_app.GetMathUtility(), "IMathUtility")
    if utility is None:
        raise RuntimeError("SOLIDWORKS returned no math utility.")
    data = list(values)
    if hasattr(utility, "_oleobj_"):
        import pythoncom
        import win32com.client

        data = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data)
    result = utility.CreateTransform(data) if kind == "transform" else utility.CreateVector(data)
    result = _typed(result, "IMathTransform" if kind == "transform" else "IMathVector")
    if result is None:
        raise RuntimeError(f"SOLIDWORKS could not create a math {kind}.")
    return result


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-9)


def _state_matches(left: dict, right: dict) -> bool:
    return (all(_close(a, b) for a, b in zip(left["orientation"], right["orientation"]))
            and all(_close(a, b) for a, b in zip(left["translation"], right["translation"]))
            and _close(left["scale"], right["scale"])
            and left["perspective"] == right["perspective"]
            and left["named_camera_active"] == right["named_camera_active"]
            and (not left["perspective"] or _close(left["object_sizes_away"], right["object_sizes_away"])))


def _restore(sw_app: Any, model: Any, before: dict) -> dict:
    errors = []
    view = _view(model)
    steps = [
        ("perspective", lambda: view.AddPerspective() if before["perspective"] else view.RemovePerspective()),
        ("orientation", lambda: setattr(view, "Orientation3", _math_object(sw_app, "transform", before["orientation"]))),
        # Orientation can reset scale; Scale2 can in turn rescale Translation3.
        # Restore the captured screen-space translation only after both settle.
        ("scale", lambda: setattr(view, "Scale2", before["scale"])),
        ("translation", lambda: setattr(view, "Translation3", _math_object(sw_app, "vector", before["translation"]))),
    ]
    if before["perspective"]:
        steps.insert(1, ("perspective_ratio", lambda: setattr(view, "ObjectSizesAway", before["object_sizes_away"])))
    for name, operation in steps:
        try:
            operation()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    try:
        model.GraphicsRedraw2()
        state = _state(model)
        if not _state_matches(state, before):
            errors.append("readback: restored model view differs from the captured state")
        return {"attempted": True, "verified": not errors, "errors": errors, "state": state}
    except Exception as exc:
        return {"attempted": True, "verified": False, "errors": [*errors, f"readback: {exc}"]}


def configure_camera(
    sw_app: Any,
    document_path: str | Path,
    *,
    action: str = "inspect",
    view: str = "isometric",
    from_direction: list | tuple | None = None,
    up_direction: list | tuple | None = None,
    fit: bool = True,
    zoom_factor: float = 1.0,
    perspective: bool = False,
    object_sizes_away: float = 3.0,
) -> dict:
    """Inspect/apply model-view framing, retaining requested state for rendering.

    ``zoom_factor`` multiplies Scale2 after optional fit; >1 moves closer.
    Perspective ``object_sizes_away`` is a dimensionless distance/size ratio,
    not a focal length. It is ignored in orthographic mode. No automatic save.
    """
    path = _document_file(document_path)
    if not isinstance(action, str) or action not in {"inspect", "apply"} or not isinstance(view, str) or view not in VIEWS:
        raise CameraError("INVALID_ARGUMENT", "Unknown camera action or view.")
    if type(fit) is not bool or type(perspective) is not bool:
        raise CameraError("INVALID_ARGUMENT", "fit and perspective must be booleans.")
    zoom = _number(zoom_factor, "zoom_factor", 0.25, 2.0)
    away = _number(object_sizes_away, "object_sizes_away", 1.0, 20.0)
    if view != "custom" and (from_direction is not None or up_direction is not None):
        raise CameraError("INVALID_ARGUMENT", "Direction vectors are only accepted for view='custom'.")
    rotation = view_rotation(from_direction, up_direction) if view == "custom" else None
    stage = "active_document"
    try:
        # COM dispatch values may be callable; do not invoke property defaults.
        model = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if model is None:
            raise CameraError("NO_ACTIVE_DOCUMENT", "No active document is available.")
        stage = "document_path"
        if not _same_path(model.GetPathName(), path):
            raise CameraError("DOCUMENT_MISMATCH", "The active saved document does not exactly match document_path.")
        stage = "document_type"
        if model.GetType() not in (1, 2):
            raise CameraError("MODEL_REQUIRED", "Camera framing supports part and assembly model documents.")
        stage = "read_view_state"
        before = _state(model)
        if action == "apply" and before["named_camera_active"]:
            raise CameraError("NAMED_CAMERA_ACTIVE", "An ICamera view is active; this tool only changes the standard model view and will not edit a named camera.")
    except CameraError:
        raise
    except Exception as exc:
        raise CameraError("CAMERA_PREFLIGHT_FAILED", f"{stage}: {exc}") from exc

    base = {"document_path": str(path), "action": action, "scope": "active_model_view", "saved": False, "before": before}
    if action == "inspect":
        return {**base, "ok": True, "mutation_attempted": False, "state": before}
    requested = {"view": view, "from_direction": list(from_direction) if from_direction is not None else None,
                 "up_direction": list(up_direction) if up_direction is not None else None,
                 "fit": fit, "zoom_factor": zoom, "perspective": perspective,
                 "object_sizes_away": away if perspective else None}
    base.update(requested=requested, mutation_attempted=True)
    stage = "orientation"
    try:
        if rotation is not None:
            _view(model).Orientation3 = _math_object(sw_app, "transform", [*rotation, 0, 0, 0, 1, 0, 0, 0])
        elif view != "current":
            model.ShowNamedView2("", STANDARD_VIEWS[view])
        native_view = _view(model)
        stage = "perspective"
        native_view.AddPerspective() if perspective else native_view.RemovePerspective()
        if perspective:
            native_view.ObjectSizesAway = away
        stage = "fit"
        if fit:
            model.ViewZoomtofit2()
        stage = "zoom"
        native_view = _view(model)
        fitted_scale = float(native_view.Scale2)
        desired_scale = fitted_scale * zoom
        if not math.isfinite(desired_scale) or desired_scale <= 0:
            raise RuntimeError("The requested view scale is not finite and positive.")
        native_view.Scale2 = desired_scale
        stage = "redraw"
        model.GraphicsRedraw2()
        stage = "readback"
        after = _state(model)
        active_model = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if active_model is None or not _same_path(active_model.GetPathName(), path):
            raise RuntimeError("The active document changed while camera settings were applied.")
        if after["perspective"] != perspective or not _close(after["scale"], desired_scale):
            raise RuntimeError("SOLIDWORKS did not retain the requested perspective or scale.")
        if perspective and not _close(after["object_sizes_away"], away):
            raise RuntimeError("SOLIDWORKS did not retain the requested perspective distance ratio.")
        if rotation is not None and not all(_close(a, b) for a, b in zip(after["orientation"][:9], rotation)):
            raise RuntimeError("SOLIDWORKS did not retain the custom orientation matrix.")
        return {**base, "ok": True, "after": after, "scale_before_zoom": fitted_scale,
                "verified": {"scale": True, "perspective": True, "custom_orientation": rotation is not None},
                "verification": "Active model-view state read back. Standard view IDs use the native API; rendered framing still requires image inspection. No named camera was created and the document was not saved."}
    except Exception as exc:
        try:
            rollback = _restore(sw_app, model, before)
        except Exception as restore_error:
            rollback = {"attempted": True, "verified": False, "errors": [str(restore_error)]}
        return {**base, "ok": False, "code": "CAMERA_APPLY_FAILED", "failed_stage": stage,
                "error": str(exc), "rollback": rollback,
                "outcome_may_be_indeterminate": not rollback["verified"]}
