"""Verified top-level component visibility for staged native assembly renders."""
from __future__ import annotations

from typing import Any

from .animation_bridge import AnimationError, _active, _name, _typed


def visibility(sw_app: Any, document_path: str, components: list[dict], *, dry_run: bool = False) -> dict:
    if type(dry_run) is not bool or not isinstance(components, list) or not (0 if dry_run else 1) <= len(components) <= 128:
        raise AnimationError("INVALID_ARGUMENT", "Supply 1..128 visibility assignments, or [] for dry_run inventory.")
    names = set()
    for item in components:
        if not isinstance(item, dict) or set(item) != {"component", "visible"} or type(item["visible"]) is not bool:
            raise AnimationError("INVALID_ARGUMENT", "Each assignment requires only component and boolean visible.")
        name = _name(item["component"], "component")
        if name in names or "/" in name or "\\" in name:
            raise AnimationError("INVALID_ARGUMENT", "Use unique exact top-level component names.")
        names.add(name)
    model, path = _active(sw_app, document_path, writable=not dry_run)
    assembly = _typed(model, "IAssemblyDoc")
    inventory = {}
    for raw in assembly.GetComponents(True) or ():
        component = _typed(raw, "IComponent2")
        name = str(component.Name2)
        if name in inventory:
            raise AnimationError("COMPONENT_NOT_UNIQUE", f"Duplicate component: {name}")
        inventory[name] = component
    for name in names:
        if name not in inventory or inventory[name].IsSuppressed():
            raise AnimationError("COMPONENT_UNAVAILABLE", f"Component missing or suppressed: {name}")
    selected = list(inventory) if dry_run and not components else [item["component"] for item in components]
    before = []
    for name in selected:
        native = int(inventory[name].Visible)
        if native not in (0, 1):
            raise AnimationError("VISIBILITY_UNKNOWN", f"Unexpected native visibility {native} for {name}.")
        before.append({"component": name, "visible": native != 0, "native_visibility": native})
    result = {"document_path": str(path), "saved": False, "rebuilt": False,
              "before": before, "mutation_attempted": False}
    if dry_run:
        return {**result, "ok": True, "dry_run": True, "components": before}
    changed = []
    try:
        for item in components:
            native = 1 if item["visible"] else 0  # swComponentVisible / swComponentHidden
            comp = inventory[item["component"]]
            if (int(comp.Visible) != 0) == item["visible"]:
                continue
            changed.append(item["component"])
            result["mutation_attempted"] = True
            comp.Visible = native
        model.GraphicsRedraw2()
        applied = [{"component": item["component"], "visible": int(inventory[item["component"]].Visible) != 0}
                   for item in components]
        if applied != components:
            raise RuntimeError("Native visibility readback differs from requested values.")
        return {**result, "ok": True, "verified": True, "applied": applied}
    except Exception as exc:
        errors = []
        original = {item["component"]: item for item in before}
        for name in reversed(changed):
            try:
                inventory[name].Visible = original[name]["native_visibility"]
            except Exception as restore_error:
                errors.append(f"{name}: {restore_error}")
        try:
            model.GraphicsRedraw2()
            verified = all(int(inventory[name].Visible) == original[name]["native_visibility"] for name in changed)
        except Exception as restore_error:
            errors.append(str(restore_error))
            verified = False
        return {**result, "ok": False, "code": "VISIBILITY_APPLY_FAILED", "error": str(exc),
                "rollback": {"attempted": bool(changed), "verified": verified and not errors, "errors": errors},
                "outcome_may_be_indeterminate": not verified or bool(errors)}
