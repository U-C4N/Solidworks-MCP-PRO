"""Internal exact state capture/restore for an owned render job on the CAD STA.

The caller holds the CAD lease until restoration finishes. These helpers never
save, rebuild, activate documents or solve mates. No COM object leaves the STA.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import camera_bridge as camera
from . import pose_bridge as pose


def _model(sw_app: Any, document_path: str | Path) -> Any:
    path = camera._document_file(document_path)
    model = camera._typed(sw_app.ActiveDoc, "IModelDoc2")
    if model is None or not camera._same_path(model.GetPathName(), path):
        raise camera.CameraError("DOCUMENT_MISMATCH", "The active document differs from the render job document.")
    if model.GetType() not in (1, 2):
        raise camera.CameraError("MODEL_REQUIRED", "Render jobs require a part or assembly.")
    return model


def camera_state(sw_app: Any, action: str, document_path: str | Path, *, state: dict | None = None) -> dict:
    """Capture/restore an unbound model view; named cameras are refused upfront."""
    if action not in {"capture", "restore"}:
        raise camera.CameraError("INVALID_ARGUMENT", "Unknown internal camera-state action.")
    model = _model(sw_app, document_path)
    if action == "capture":
        current = camera._state(model)
        if current["named_camera_active"]:
            raise camera.CameraError("NAMED_CAMERA_ACTIVE", "Deactivate the named camera before running a render job.")
        return {"ok": True, "state": current, "saved": False}
    if not isinstance(state, dict) or state.get("named_camera_active") is not False:
        raise camera.CameraError("INVALID_ARGUMENT", "An unbound captured camera state is required.")
    camera._array(state.get("orientation"), 16, "orientation")
    camera._array(state.get("translation"), 3, "translation")
    camera._number(state.get("scale"), "scale", 1e-30, 1e30)
    if type(state.get("perspective")) is not bool:
        raise camera.CameraError("INVALID_ARGUMENT", "Captured perspective must be boolean.")
    if state["perspective"]:
        camera._number(state.get("object_sizes_away"), "object_sizes_away", 1e-30, 1e30)
    restored = camera._restore(sw_app, model, state)
    return {"ok": restored["verified"], "saved": False, **restored}


def restore_poses(sw_app: Any, document_path: str | Path, *, states: list[dict]) -> dict:
    """Restore raw 16-element native matrices, including singular Euler poses."""
    model = _model(sw_app, document_path)
    if model.GetType() != 2:
        raise pose.PoseError("ASSEMBLY_REQUIRED", "Pose restoration requires an assembly.")
    if not isinstance(states, list) or not 1 <= len(states) <= 128:
        raise pose.PoseError("INVALID_ARGUMENT", "Expected 1..128 captured component states.")
    requested = {}
    for state in states:
        if not isinstance(state, dict) or not isinstance(state.get("component"), str):
            raise pose.PoseError("INVALID_ARGUMENT", "Each captured state requires a component and matrix.")
        name = state["component"]
        if not name or name in requested or "/" in name or "\\" in name:
            raise pose.PoseError("INVALID_ARGUMENT", "Captured names must be unique top-level instance names.")
        requested[name] = pose._array(state.get("transform"), 16, "captured transform")
    assembly = pose._typed(model, "IAssemblyDoc")
    raw = assembly.GetComponents(True) or ()
    raw = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    components = {}
    for item in raw:
        component = pose._typed(item, "IComponent2")
        name = str(component.Name2)
        if name in components:
            raise pose.PoseError("COMPONENT_NOT_UNIQUE", f"Duplicate component {name!r} during cleanup.")
        components[name] = component
    for name in requested:
        if name not in components or components[name].IsSuppressed():
            raise pose.PoseError("POSE_RESTORE_TARGET_MISSING", f"Cannot restore unavailable component {name!r}.")
    utility = pose._typed(sw_app.GetMathUtility(), "IMathUtility")
    prepared = {name: pose._transform(utility, values) for name, values in requested.items()}
    errors = []
    for name, transform in prepared.items():
        try:
            components[name].Transform2 = transform
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    try:
        model.GraphicsRedraw2()
    except Exception as exc:
        errors.append(f"redraw: {exc}")
    after = []
    for name, expected in requested.items():
        try:
            actual = pose._data(components[name])
            after.append({"component": name, "transform": actual})
            if not pose._matches(actual, expected):
                errors.append(f"{name}: restored matrix readback differs")
        except Exception as exc:
            errors.append(f"{name}: readback: {exc}")
    return {"ok": not errors, "attempted": True, "verified": not errors,
            "errors": errors, "state": after, "saved": False, "rebuilt": False}
