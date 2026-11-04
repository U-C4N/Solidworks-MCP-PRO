"""Absolute top-level assembly poses on the owning CAD STA, without rebuilding.

The caller holds the shared CAD lease. These are explicit kinematic placements,
not a mate solver or physics simulation. Existing mates may override poses on a
later rebuild. Use an authorized animation copy; this module never saves it.
"""
from __future__ import annotations

from array import array
import math
import os
from pathlib import Path
import stat
from typing import Any


class PoseError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _typed(obj: Any, interface: str) -> Any:
    if obj is None or not hasattr(obj, "_oleobj_"):
        return obj
    from .native import binding
    return binding.wrap(obj, getattr(binding.module(), interface))


def _vector(value: Any, label: str, bound: float) -> list[float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(type(v) not in (int, float) or not -bound <= v <= bound or not math.isfinite(v) for v in value)):
        raise PoseError("INVALID_ARGUMENT", f"{label} must contain three finite numbers in [-{bound:g}, {bound:g}].")
    return [float(v) for v in value]


def rotation_columns(rotation_deg: list[float]) -> list[float]:
    """XYZ rotations about assembly axes: Rz Ry Rx, native column-major array."""
    x, y, z = (math.radians(v) for v in rotation_deg)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    return [cz * cy, sz * cy, -sy,
            cz * sy * sx - sz * cx, sz * sy * sx + cz * cx, cy * sx,
            cz * sy * cx + sz * sx, sz * sy * cx - cz * sx, cy * cx]


def _euler(columns: list[float]) -> list[float]:
    y = math.asin(max(-1.0, min(1.0, -columns[2])))
    if abs(columns[2]) > 1.0 - 1e-9:
        x, z = math.atan2(-columns[7], columns[4]), 0.0
    else:
        x, z = math.atan2(columns[5], columns[8]), math.atan2(columns[1], columns[0])
    return [math.degrees(v) for v in (x, y, z)]


def _array(value: Any, length: int, label: str) -> list[float]:
    if (not isinstance(value, (list, tuple, array)) or len(value) != length
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
        raise RuntimeError(f"Native {label} is not a finite {length}-element array.")
    return [float(v) for v in value]


def _rigid(data: list[float]) -> bool:
    columns = [data[index:index + 3] for index in (0, 3, 6)]
    if not math.isclose(data[12], 1.0, abs_tol=1e-9, rel_tol=0):
        return False
    for i in range(3):
        for j in range(i, 3):
            if abs(sum(a * b for a, b in zip(columns[i], columns[j])) - (1.0 if i == j else 0.0)) > 1e-6:
                return False
    a, b, c = columns
    determinant = a[0] * (b[1] * c[2] - b[2] * c[1]) - b[0] * (a[1] * c[2] - a[2] * c[1]) + c[0] * (a[1] * b[2] - a[2] * b[1])
    return abs(determinant - 1.0) <= 1e-6


def _data(component: Any) -> list[float]:
    transform = _typed(component.Transform2, "IMathTransform")
    if transform is None:
        raise RuntimeError(f"No readable Transform2 for {component.Name2!r}.")
    return _array(transform.ArrayData, 16, "Transform2")


def _snapshot(component: Any) -> dict:
    data = _data(component)
    rigid = _rigid(data)
    result = {"component": str(component.Name2), "transform": data,
              "position_mm": [v * 1000 for v in data[9:12]],
              "rotation_deg": _euler(data[:9]) if rigid else None, "rigid_unit_scale": rigid}
    try:
        bounds = component.GetBox(False, False)
        result["bounds_mm"] = [v * 1000 for v in _array(bounds, 6, "component bounds")] if bounds is not None else None
        result["bounds_kind"] = "native_component_box_approximate_assembly_axes"
    except Exception as exc:
        result.update(bounds_mm=None, bounds_error=str(exc))
    return result


def _transform(utility: Any, values: list[float]) -> Any:
    data: Any = list(values)
    if hasattr(utility, "_oleobj_"):
        import pythoncom
        import win32com.client
        data = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data)
    transform = _typed(utility.CreateTransform(data), "IMathTransform")
    if transform is None:
        raise RuntimeError("SOLIDWORKS CreateTransform returned no object.")
    if not _matches(_array(transform.ArrayData, 16, "created transform"), values):
        raise RuntimeError("SOLIDWORKS CreateTransform changed the requested matrix.")
    return transform


def _matches(actual: list[float], expected: list[float]) -> bool:
    # Absolute tolerance: 1e-9 for rotations, 1e-9 metres = 1e-6 mm for positions.
    return len(actual) == len(expected) == 16 and all(abs(a - b) <= 1e-9 for a, b in zip(actual, expected))


def _same_path(actual: str, expected: Path) -> bool:
    try:
        return isinstance(actual, str) and os.path.samefile(actual, expected)
    except (OSError, ValueError):
        return False


def pose(sw_app: Any, document_path: str | Path, transforms: list[dict], *, dry_run: bool = False) -> dict:
    """Apply 1..128 exact component poses; [] with dry_run inventories all.

    Each item is {component: exact Name2, position_mm: [x,y,z], rotation_deg:
    [rx,ry,rz]}. Positions and rotations are absolute in assembly coordinates.
    All targets, snapshots and native transforms are prepared before any write.
    No EditRebuild/ForceRebuild/Save method is called, including during rollback.
    """
    if type(dry_run) is not bool:
        raise PoseError("INVALID_ARGUMENT", "dry_run must be boolean.")
    if (not isinstance(document_path, (str, Path)) or not str(document_path) or "\x00" in str(document_path)):
        raise PoseError("INVALID_ARGUMENT", "document_path must identify an existing absolute local assembly.")
    path = Path(document_path)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() != ".sldasm" or not path.is_file() or path.stat().st_size == 0):
        raise PoseError("INVALID_ARGUMENT", "document_path must be an absolute existing nonempty local .sldasm.")
    if not isinstance(transforms, list) or not (0 if dry_run else 1) <= len(transforms) <= 128:
        raise PoseError("INVALID_ARGUMENT", "Supply 1..128 poses, or an empty list only for dry_run inventory.")
    requests, names = [], set()
    for item in transforms:
        if not isinstance(item, dict) or set(item) != {"component", "position_mm", "rotation_deg"}:
            raise PoseError("INVALID_ARGUMENT", "Each pose requires only component, position_mm and rotation_deg.")
        name = item["component"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 512
                or any(ord(c) < 32 for c in name) or "/" in name or "\\" in name or name in names):
            raise PoseError("INVALID_ARGUMENT", "Component names must be unique exact top-level instance names.")
        names.add(name)
        position = _vector(item["position_mm"], "position_mm", 1e6)
        rotation = _vector(item["rotation_deg"], "rotation_deg", 3600)
        requests.append({"component": name, "position_mm": position, "rotation_deg": rotation})

    stage = "active_document"
    try:
        model = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if model is None:
            raise PoseError("NO_ACTIVE_DOCUMENT", "No active saved assembly.")
        if not _same_path(model.GetPathName(), path):
            raise PoseError("DOCUMENT_MISMATCH", "The active saved document does not match document_path.")
        if model.GetType() != 2:
            raise PoseError("ASSEMBLY_REQUIRED", "Poses apply only to an assembly document.")
        read_only = bool(model.IsOpenedReadOnly() or model.IsOpenedViewOnly() or not path.stat().st_mode & stat.S_IWRITE)
        if read_only and not dry_run:
            raise PoseError("DOCUMENT_READ_ONLY", "The assembly is read-only or view-only.")
        stage = "component_inventory"
        assembly = _typed(model, "IAssemblyDoc")
        raw = assembly.GetComponents(True) or ()
        raw = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        if len(raw) > 10000:
            raise PoseError("ASSEMBLY_TOO_LARGE", "Top-level inventory exceeds 10000 components.")
        components = {}
        for item in raw:
            component = _typed(item, "IComponent2")
            name = str(component.Name2)
            if name in components:
                raise PoseError("COMPONENT_NOT_UNIQUE", f"Duplicate native component name {name!r}.")
            components[name] = component
        for name in names:
            if name not in components:
                raise PoseError("COMPONENT_NOT_FOUND", f"No exact top-level component {name!r}.")
            if components[name].IsSuppressed():
                raise PoseError("COMPONENT_SUPPRESSED", f"Target {name!r} is suppressed.")
        selected = list(components) if dry_run and not requests else [item["component"] for item in requests]
        before = []
        for name in selected:
            component = components[name]
            if dry_run and component.IsSuppressed():
                before.append({"component": name, "suppressed": True, "transform": None,
                               "position_mm": None, "rotation_deg": None, "bounds_mm": None})
            else:
                before.append({**_snapshot(component), "suppressed": False})
        if any(not item.get("rigid_unit_scale", False) for item in before) and not dry_run:
            raise PoseError("NON_RIGID_COMPONENT", "Scaled or reflected component transforms cannot be represented by XYZ poses.")
        if dry_run:
            return {"ok": True, "dry_run": True, "document_path": str(path), "read_only": read_only,
                    "mutation_attempted": False, "saved": False, "rebuilt": False,
                    "components": before, "requested": requests}
        stage = "prepare_transforms"
        utility = _typed(sw_app.GetMathUtility(), "IMathUtility")
        if utility is None:
            raise RuntimeError("No native math utility.")
        wanted = {item["component"]: rotation_columns(item["rotation_deg"]) +
                  [v / 1000 for v in item["position_mm"]] + [1.0, 0.0, 0.0, 0.0] for item in requests}
        prepared = {name: _transform(utility, data) for name, data in wanted.items()}
    except PoseError:
        raise
    except Exception as exc:
        raise PoseError("POSE_PREFLIGHT_FAILED", f"{stage}: {exc}") from exc

    base = {"document_path": str(path), "requested": requests, "before": before,
            "saved": False, "rebuilt": False, "mutation_attempted": True}
    applied = []
    try:
        for name, transform in prepared.items():
            stage = f"apply:{name}"
            components[name].Transform2 = transform
            applied.append(name)
        stage = "redraw"
        model.GraphicsRedraw2()
        stage = "verify"
        active = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if active is None or not _same_path(active.GetPathName(), path):
            raise RuntimeError("Active document changed while poses were applied.")
        after = [_snapshot(components[name]) for name in wanted]
        for state in after:
            if not _matches(state["transform"], wanted[state["component"]]):
                raise RuntimeError(f"Transform readback differs for {state['component']!r}; existing mates may constrain it.")
        return {**base, "ok": True, "verified": True, "after": after,
                "note": "Absolute kinematic poses verified. Mates are not solved; a later rebuild can override them."}
    except Exception as exc:
        errors = []
        for state in reversed(before):
            try:
                components[state["component"]].Transform2 = _transform(utility, state["transform"])
            except Exception as restore_exc:
                errors.append(f"{state['component']}: {restore_exc}")
        try:
            model.GraphicsRedraw2()
        except Exception as redraw_exc:
            errors.append(f"redraw: {redraw_exc}")
        restored = []
        for state in before:
            try:
                actual = _snapshot(components[state["component"]])
                restored.append(actual)
                if not _matches(actual["transform"], state["transform"]):
                    errors.append(f"{state['component']}: rollback transform readback differs")
            except Exception as read_exc:
                errors.append(f"{state['component']}: rollback readback: {read_exc}")
        return {**base, "ok": False, "code": "POSE_APPLY_FAILED", "failed_stage": stage, "error": str(exc),
                "applied_before_failure": applied,
                "rollback": {"attempted": True, "verified": not errors, "errors": errors, "state": restored},
                "outcome_may_be_indeterminate": bool(errors)}
