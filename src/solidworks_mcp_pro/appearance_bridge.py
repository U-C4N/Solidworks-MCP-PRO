"""Apply native render appearances to assembly instances on the owning CAD STA.

No part documents are opened or mutated, no automatic save is performed, and
there is no claimed rollback if SOLIDWORKS partially accepts an operation.
The caller must hold the CAD writer lease for the complete call.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any


class AppearanceError(RuntimeError):
    """Preflight failure before any appearance mutation is attempted."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _typed(obj: Any, interface: str) -> Any:
    if obj is None or not hasattr(obj, "_oleobj_"):
        return obj
    # Use the independent session's matching early-bound SDK wrappers.
    # Do not attach to another instance or ask default dispatch for type info.
    from .native import binding

    return binding.wrap(obj, getattr(binding.module(), interface))


def _file(value: Any, suffix: str, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise AppearanceError("INVALID_ARGUMENT", f"{label} must be an absolute existing {suffix} file.")
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//"))
            or path.suffix.lower() != suffix or path.is_symlink() or not path.is_file()):
        raise AppearanceError("INVALID_ARGUMENT", f"{label} must be an absolute local existing {suffix} file: {value!r}.")
    if path.stat().st_size == 0:
        raise AppearanceError("INVALID_ARGUMENT", f"{label} is empty: {path}.")
    return path


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _ids(value: Any) -> tuple[int, int]:
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(type(item) is not int for item in value)):
        raise RuntimeError(f"Native material IDs have an unexpected shape: {value!r}.")
    return value[0], value[1]


def _describe_collection(owner: Any, scope: str) -> dict:
    result = {"scope": scope, "materials": []}
    try:
        items = owner.GetRenderMaterials2(1, None) or ()
        result["count"] = len(items)
        for item in items:
            material = _typed(item, "IRenderMaterial")
            description = {}
            result["materials"].append(description)
            try:
                description["material_ids"] = list(_ids(material.GetMaterialIds()))
                description["material_path"] = str(material.FileName)
                description["primary_color"] = int(material.PrimaryColor)
                description["entity_count"] = material.GetEntitiesCount()
                description["entities"] = []
                for entity in material.GetEntities() or ():
                    try:
                        entity = _typed(entity, "IComponent2")
                        description["entities"].append({"component": str(entity.Name2)})
                    except Exception as exc:
                        description["entities"].append({"component_name_error": str(exc)})
            except Exception as exc:
                description["error"] = str(exc)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _readback(extension: Any, target_component: Any, ids: tuple[int, int], component: str,
              color: int | None, requested_path: Path) -> dict:
    found = []
    observations = []
    # Live SW2026 evidence: AddDisplayStateSpecificRenderMaterial and the fresh
    # assembly collection can report different IDs for the same applied state.
    # Verify the unique assembly appearance by actual file identity + exact
    # component entity + requested color. Component collections are diagnostic
    # only: they may expose the referenced part's pre-existing appearance.
    for scope, owner in (("component", target_component), ("assembly_extension", extension)):
        observation = {"scope": scope, "material_ids": []}
        observations.append(observation)
        try:
            items = owner.GetRenderMaterials2(1, None) or ()
            observation["count"] = len(items)
            for item in items:
                material = _typed(item, "IRenderMaterial")
                actual_ids = _ids(material.GetMaterialIds())
                observation["material_ids"].append(list(actual_ids))
                if scope == "assembly_extension":
                    found.append((scope, material, actual_ids))
        except Exception as exc:
            observation["error"] = str(exc)
    failures = []
    verified = []
    for scope, material, actual_ids in found:
        try:
            actual_path = str(material.FileName)
            if not os.path.samefile(actual_path, requested_path):
                raise RuntimeError(f"Material file differs: {actual_path!r}.")
            count = material.GetEntitiesCount()
            names = []
            for entity in material.GetEntities() or ():
                entity = _typed(entity, "IComponent2")
                names.append(str(entity.Name2))
            if count != len(names) or component not in names:
                raise RuntimeError(f"Entity readback differs: count={count!r}, components={names!r}.")
            actual_color = int(material.PrimaryColor)
            if color is not None and actual_color != color:
                raise RuntimeError(f"Color readback differs: expected={color}, actual={actual_color}.")
            verified.append({"material_ids": list(actual_ids), "accepted_material_ids": list(ids),
                             "id_scope_mismatch": actual_ids != ids,
                             "entity_count": count, "components": names,
                             "primary_color": actual_color, "material_path": actual_path,
                             "source": scope, "collections": observations})
        except Exception as exc:
            failures.append({"scope": scope, "error": str(exc)})
    if len(verified) == 1:
        return verified[0]
    raise RuntimeError(f"Accepted material IDs {ids} have {len(verified)} matching assembly file/entity/color readbacks; expected one. "
                       f"Observed collections: {observations!r}; candidate failures: {failures!r}.")


def apply(
    sw_app: Any,
    document_path: str | Path,
    assignments: list[dict],
    scene_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Apply to exact assembly instance names, in the current display state only.

    color_rgb uses three integers in 0..255. Preflight failures raise
    AppearanceError; failures after native mutation starts return explicit
    partial-state evidence. Source/part files are never saved by this function.
    Scene editing is intentionally unsupported in this first version.
    """
    if scene_path is not None:
        raise AppearanceError("SCENE_UNSUPPORTED", "Scene editing is not supported by this appearance operation; omit scene_path.")
    if type(dry_run) is not bool:
        raise AppearanceError("INVALID_ARGUMENT", "dry_run must be a boolean.")
    path = _file(document_path, ".sldasm", "document_path")
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 128:
        raise AppearanceError("INVALID_ARGUMENT", "assignments must contain 1–128 objects.")

    # Validate every caller-controlled value before requesting any native mutation.
    validated = []
    used = set()
    for index, assignment in enumerate(assignments):
        if (not isinstance(assignment, dict)
                or not {"component", "material_path"} <= assignment.keys()
                or assignment.keys() - {"component", "material_path", "color_rgb"}):
            raise AppearanceError("INVALID_ARGUMENT", f"Assignment {index} requires component/material_path and optional color_rgb only.")
        name = assignment["component"]
        if not isinstance(name, str) or not name.strip() or "\x00" in name or name in used:
            raise AppearanceError("INVALID_ARGUMENT", f"Assignment {index} needs a unique, exact nonempty component name.")
        used.add(name)
        material_path = _file(assignment["material_path"], ".p2m", f"assignments[{index}].material_path")
        color = assignment.get("color_rgb")
        if "color_rgb" in assignment:
            if (not isinstance(color, (list, tuple)) or len(color) != 3
                    or any(type(channel) is not int or not 0 <= channel <= 255 for channel in color)):
                raise AppearanceError("INVALID_ARGUMENT", f"Assignment {index} color_rgb must contain three integers in 0..255.")
            packed_color = color[0] | (color[1] << 8) | (color[2] << 16)
        else:
            packed_color = None
        validated.append((name, material_path, packed_color))

    preflight_stage = "active_document"
    try:
        # COM property values can themselves be callable dispatch objects.
        # Calling them would invoke their default DISPID, not read the property.
        model = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if model is None:
            raise AppearanceError("NO_ACTIVE_DOCUMENT", "No active assembly is available.")
        preflight_stage = "document_path"
        active_path = model.GetPathName()
        if not active_path or not _same_path(active_path, path):
            raise AppearanceError("DOCUMENT_MISMATCH", "The active saved document does not match document_path.")
        preflight_stage = "document_type"
        if model.GetType() != 2:
            raise AppearanceError("ASSEMBLY_REQUIRED", "Only an active saved assembly supports component appearances.")
        preflight_stage = "read_only_state"
        if (model.IsOpenedReadOnly() or model.IsOpenedViewOnly()
                or not path.stat().st_mode & stat.S_IWRITE):
            raise AppearanceError("DOCUMENT_READ_ONLY", "The target assembly is read-only or view-only.")
        preflight_stage = "assembly_interface"
        assembly = _typed(model, "IAssemblyDoc")
        preflight_stage = "model_extension"
        extension = _typed(model.Extension, "IModelDocExtension")
        components = {}
        preflight_stage = "component_enumeration"
        for item in assembly.GetComponents(False) or ():
            component = _typed(item, "IComponent2")
            name = str(component.Name2)
            components.setdefault(name, []).append(component)
        prepared = []
        for name, material_path, color in validated:
            matches = components.get(name, [])
            if len(matches) != 1:
                raise AppearanceError("COMPONENT_NOT_UNIQUE", f"Expected exactly one component named {name!r}; found {len(matches)}.")
            prepared.append((name, material_path, color, matches[0]))
        preflight_stage = "current_display_material_count"
        before_count = extension.GetRenderMaterialsCount2(1, None)
    except AppearanceError:
        raise
    except Exception as exc:
        raise AppearanceError("APPEARANCE_PREFLIGHT_FAILED", f"{preflight_stage}: {exc}") from exc

    if dry_run:
        return {"ok": True, "dry_run": True, "document_path": str(path),
                "display_state_scope": "current", "saved": False, "mutation_attempted": False,
                "assembly": _describe_collection(extension, "assembly_extension"),
                "components": [{"component": name, "requested_material_path": str(material_path),
                                "requested_primary_color": color,
                                "collection": _describe_collection(component, "component")}
                               for name, material_path, color, component in prepared]}

    records = []
    base = {"document_path": str(path), "display_state_scope": "current", "saved": False,
            "rollback_performed": False, "material_count_before": before_count, "applied": records}
    for name, material_path, color, component in prepared:
        stage = "create_render_material"
        record = {"component": name, "requested_material_path": str(material_path),
                  "accepted": False, "verified": False}
        records.append(record)
        try:
            material = _typed(extension.CreateRenderMaterial(str(material_path)), "IRenderMaterial")
            if material is None:
                raise RuntimeError("CreateRenderMaterial returned no material.")
            if color is not None:
                stage = "set_color"
                material.PrimaryColor = color
                if int(material.PrimaryColor) != color:
                    raise RuntimeError("PrimaryColor did not retain the requested RGB value.")
            stage = "add_component_entity"
            if not material.AddEntity(component):
                raise RuntimeError("AddEntity rejected the assembly component.")
            record["entity_accepted"] = True
            try:
                record["material_ids_before_apply"] = list(_ids(material.GetMaterialIds()))
            except Exception as exc:
                record["material_ids_before_apply_error"] = str(exc)
            stage = "apply_current_display_state"
            native_result = extension.AddDisplayStateSpecificRenderMaterial(material, 1, None)
            record["native_result"] = (list(native_result) if isinstance(native_result, (tuple, list))
                                       and all(type(value) in (bool, int) for value in native_result)
                                       else repr(native_result))
            if not isinstance(native_result, (tuple, list)) or len(native_result) != 3:
                raise RuntimeError(f"Unexpected native application result: {native_result!r}.")
            # Capture IDs even when the native method reports failure.
            ids = _ids(native_result[1:])
            record["material_ids"] = list(ids)
            if not native_result[0]:
                raise RuntimeError("AddDisplayStateSpecificRenderMaterial returned false.")
            record["accepted"] = True
            stage = "returned_material_ids"
            record["material_ids_after_apply"] = list(_ids(material.GetMaterialIds()))
            stage = "readback"
            record["readback"] = _readback(extension, component, ids, name, color, material_path)
            record["verified"] = True
        except Exception as exc:
            return {**base, "ok": False, "code": "APPEARANCE_PARTIAL_FAILURE",
                    "mutation_attempted": True, "partial_state_possible": True,
                    "failed": {"component": name, "stage": stage, "error": str(exc)},
                    "error": "Native appearance application stopped. Prior changes may remain; no rollback or save was performed."}
    return {**base, "ok": True, "mutation_attempted": True, "partial_state_possible": False,
            "verification": "Current-display appearance IDs, assembly entities and requested RGB read back; Visualize fidelity still requires render inspection."}


apply_appearances = apply
