"""Verified RGB batches on the owning CAD STA, without saving or rebuilding.

The caller holds the CAD lease and one command guard for this complete call.
Component colors are current-configuration assembly overrides; referenced
documents are read only to obtain inherited coefficients, never changed.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import stat
from typing import Any


_DEFAULT = [0.8, 0.8, 0.8, 1.0, 1.0, 0.5, 0.4, 0.0, 0.0]
_TOLERANCE = 0.5 / 255 + 1e-9
_THIS_CONFIGURATION = 1


class ColorError(RuntimeError):
    """A batch was rejected before any native appearance write."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _typed(obj: Any, interface: str) -> Any:
    if obj is None or not hasattr(obj, "_oleobj_"):
        return obj
    from .native import binding
    return binding.wrap(obj, getattr(binding.module(), interface))


def _same_file(left: str, right: Path) -> bool:
    try:
        return bool(left) and os.path.samefile(left, right)
    except (OSError, ValueError):
        return False


def _array(value: Any) -> list[float]:
    if (not isinstance(value, (tuple, list)) or len(value) != 9
            or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)):
        raise RuntimeError("Native appearance is not a finite nine-value array.")
    values = [float(item) for item in value]
    if not all(item == -1 for item in values) and any(not 0 <= item <= 1 for item in values):
        raise RuntimeError("Native appearance must contain values in 0..1, or nine -1 sentinels.")
    return values


def _absent(values: list[float]) -> bool:
    return all(value == -1 for value in values)


def _matches(left: list[float], right: list[float]) -> bool:
    if _absent(left) or _absent(right):
        return left == right
    # RGB is stored in 1/255 steps; a whole-channel mismatch must still fail.
    return all(abs(a - b) <= _TOLERANCE for a, b in zip(left, right))


def _doubles(owner: Any, values: list[float]) -> Any:
    if not hasattr(owner, "_oleobj_"):
        return values[:]
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, values)


def _read(owner: Any, component: str | None) -> list[float]:
    if component is None:
        return _array(owner.MaterialPropertyValues)
    return _array(owner.GetMaterialPropertyValues2(_THIS_CONFIGURATION, None))


def _write(owner: Any, component: str | None, values: list[float]) -> None:
    if component is None:
        owner.MaterialPropertyValues = _doubles(owner, values)
    elif owner.SetMaterialPropertyValues2(_doubles(owner, values), _THIS_CONFIGURATION, None) is False:
        raise RuntimeError("SetMaterialPropertyValues2 returned false.")


def _remove(owner: Any, component: str | None) -> None:
    if component is None:
        accepted = _typed(owner.Extension, "IModelDocExtension").RemoveMaterialProperty(_THIS_CONFIGURATION, None)
    else:
        accepted = owner.RemoveMaterialProperty2(_THIS_CONFIGURATION, None)
    if not accepted:
        raise RuntimeError("Native appearance removal returned false.")


def _state(name: str | None, values: list[float]) -> dict:
    return {"component": name, "has_override": not _absent(values), "values": values[:],
            "color_rgb": None if _absent(values) else [round(value * 255) for value in values[:3]]}


def colorize(sw_app: Any, document_path: str | Path, assignments: list[dict], *, dry_run: bool = False) -> dict:
    """Apply 1..128 exact top-level RGB assignments after complete preflight.

    Omit component for a whole part. Assemblies require an exact top-level
    instance name. Failures after writing return rollback evidence; preflight
    failures raise ColorError. No document is opened, saved, or rebuilt.
    """
    if type(dry_run) is not bool or not isinstance(assignments, list) or not 1 <= len(assignments) <= 128:
        raise ColorError("INVALID_ARGUMENT", "Supply 1..128 assignments and a boolean dry_run.")
    if not isinstance(document_path, (str, Path)) or not str(document_path) or "\x00" in str(document_path):
        raise ColorError("INVALID_ARGUMENT", "document_path must name an existing absolute local CAD file.")
    path = Path(document_path)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() not in {".sldprt", ".sldasm"} or not path.is_file() or not path.stat().st_size):
        raise ColorError("INVALID_ARGUMENT", "document_path must name an existing absolute local .sldprt or .sldasm file.")
    wanted = []
    names = set()
    for item in assignments:
        if not isinstance(item, dict) or "color_rgb" not in item or item.keys() - {"component", "color_rgb"}:
            raise ColorError("INVALID_ARGUMENT", "Each assignment requires color_rgb and an optional exact component name only.")
        name = item.get("component")
        if "component" in item and (not isinstance(name, str) or not name.strip() or len(name) > 1024
                                    or any(char in name for char in ("\x00", "/", "\\"))):
            raise ColorError("INVALID_ARGUMENT", "component must be an exact top-level instance name.")
        rgb = item["color_rgb"]
        if (not isinstance(rgb, (list, tuple)) or len(rgb) != 3
                or any(type(channel) is not int or not 0 <= channel <= 255 for channel in rgb)):
            raise ColorError("INVALID_ARGUMENT", "color_rgb must contain three integers in 0..255.")
        if name in names:
            raise ColorError("INVALID_ARGUMENT", "Each color target must be unique in a batch.")
        names.add(name)
        wanted.append((name, list(rgb)))

    stage = "active_document"
    try:
        model = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if model is None:
            raise ColorError("NO_ACTIVE_DOCUMENT", "No active saved CAD document.")
        active_path = str(model.GetPathName())
        if not _same_file(active_path, path):
            raise ColorError("DOCUMENT_MISMATCH", "The active saved document does not match document_path.")
        doc_type = int(model.GetType())
        if doc_type not in (1, 2) or path.suffix.lower() != (".sldprt" if doc_type == 1 else ".sldasm"):
            raise ColorError("DOCUMENT_TYPE", "The active document must match the requested part or assembly type.")
        if not dry_run and (model.IsOpenedReadOnly() or model.IsOpenedViewOnly()
                            or not path.stat().st_mode & stat.S_IWRITE):
            raise ColorError("DOCUMENT_READ_ONLY", "The active document is read-only or view-only.")
        if doc_type == 1 and names != {None}:
            raise ColorError("PART_COMPONENT", "Omit component when coloring a whole part.")
        if doc_type == 2 and None in names:
            raise ColorError("COMPONENT_REQUIRED", "Assemblies require exact top-level component names.")
        stage = "component_inventory"
        inventory = {}
        if doc_type == 2:
            for raw in _typed(model, "IAssemblyDoc").GetComponents(True) or ():
                owner = _typed(raw, "IComponent2")
                name = str(owner.Name2)
                inventory.setdefault(name, []).append(owner)
        stage = "appearance_snapshot"
        prepared = []
        for name, rgb in wanted:
            if name is None:
                owner = model
            else:
                matches = inventory.get(name, [])
                if len(matches) != 1:
                    raise ColorError("COMPONENT_NOT_UNIQUE", f"Expected one top-level component {name!r}; found {len(matches)}.")
                owner = matches[0]
                if owner.IsSuppressed():
                    raise ColorError("COMPONENT_UNAVAILABLE", f"Component is suppressed: {name!r}.")
            before = _read(owner, name)
            source = before
            if _absent(source):
                inherited = _typed(owner.GetModelDoc2(), "IModelDoc2") if name is not None else None
                source = _array(inherited.MaterialPropertyValues) if inherited is not None else _DEFAULT
                if _absent(source):
                    source = _DEFAULT
            desired = [channel / 255 for channel in rgb] + source[3:]
            prepared.append({"component": name, "owner": owner, "before": before, "desired": desired})
    except ColorError:
        raise
    except Exception as exc:
        raise ColorError("COLOR_PREFLIGHT_FAILED", f"{stage}: {exc}") from exc

    before_states = [_state(item["component"], item["before"]) for item in prepared]
    base = {"document_path": str(path), "configuration_scope": "current", "saved": False, "rebuilt": False,
            "before": before_states, "mutation_attempted": False}
    skipped = [item["component"] for item in prepared if _matches(item["before"], item["desired"])]
    if dry_run:
        return {**base, "ok": True, "dry_run": True, "skipped": skipped,
                "requested": [{"component": name, "color_rgb": rgb} for name, rgb in wanted]}

    touched = []
    applied = []
    try:
        for item in prepared:
            name, owner = item["component"], item["owner"]
            if name in skipped:
                continue
            stage = f"apply:{name if name is not None else 'part'}"
            touched.append(item)  # A failing native setter may already have changed the target.
            base["mutation_attempted"] = True
            _write(owner, name, item["desired"])
            stage = f"readback:{name if name is not None else 'part'}"
            actual = _read(owner, name)
            if not _matches(actual, item["desired"]):
                raise RuntimeError(f"Native appearance readback differs for {name!r}.")
            applied.append(_state(name, actual))
        if touched:
            stage = "redraw"
            model.GraphicsRedraw2()
        stage = "final_readback"
        active = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if active is None or not _same_file(str(active.GetPathName()), path):
            raise RuntimeError("The active document changed while colors were applied.")
        after = []
        for item in prepared:
            actual = _read(item["owner"], item["component"])
            if not _matches(actual, item["desired"]):
                raise RuntimeError(f"Final native appearance differs for {item['component']!r}.")
            after.append(_state(item["component"], actual))
        return {**base, "ok": True, "verified": True, "after": after, "skipped": skipped,
                "changed_count": len(touched), "skipped_count": len(skipped), "partial_state_possible": False}
    except Exception as exc:
        errors = []
        for item in reversed(touched):
            try:
                if _absent(item["before"]):
                    _remove(item["owner"], item["component"])
                else:
                    _write(item["owner"], item["component"], item["before"])
            except Exception as restore_exc:
                errors.append(f"{item['component']!r}: restore call: {restore_exc}")
        if touched:
            try:
                model.GraphicsRedraw2()
            except Exception as redraw_exc:
                errors.append(f"rollback redraw: {redraw_exc}")
        restored = []
        verified = True
        for item in touched:
            try:
                actual = _read(item["owner"], item["component"])
                restored.append(_state(item["component"], actual))
                if not _matches(actual, item["before"]):
                    verified = False
                    errors.append(f"{item['component']!r}: rollback appearance readback differs")
            except Exception as read_exc:
                verified = False
                errors.append(f"{item['component']!r}: rollback readback: {read_exc}")
        return {**base, "ok": False, "code": "COLOR_APPLY_FAILED", "failed_stage": stage,
                "error": str(exc), "applied_before_failure": applied, "skipped": skipped,
                "rollback": {"attempted": bool(touched), "verified": verified, "errors": errors, "state": restored},
                "partial_state_possible": not verified, "outcome_may_be_indeterminate": not verified}
