"""Explicit design assertions, evaluated on the caller's CAD STA and lease.

No document activation, rebuild, save, selection change or inferred reference is
performed. A failed requirement is a completed check; an unavailable native
measurement is an unknown result and can never satisfy an assertion.
"""
from __future__ import annotations

from copy import deepcopy
import math
import os
from pathlib import Path
from typing import Any

from mcp import types

from .camera_bridge import _typed
from .pose_bridge import _rigid


KINDS = {"dimension", "mass", "volume", "bbox_size", "distance"}
UNITS = {"mass": "kg", "volume": "mm3", "bbox_size": "mm", "distance": "mm"}
MAX_ASSERTIONS = 128
MAX_NATIVE_ITEMS = 10000


class DesignValidationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _number(value: Any, label: str, *, nonnegative: bool = False) -> float:
    try:
        if type(value) not in (int, float):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or (nonnegative and number < 0):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise DesignValidationError("INVALID_ARGUMENT", f"{label} must be a finite "
                                    f"{'nonnegative ' if nonnegative else ''}number.") from None
    return number


def _text(value: Any, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise DesignValidationError("INVALID_ARGUMENT", f"{label} must be a nonempty string of at most {maximum} characters.")
    return value


def validate_assertions(assertions: Any) -> list[dict]:
    """Preflight the complete assertion list without touching CAD or files.

    Each item requires id/kind/unit/abs_tolerance and either expected or min/max.
    Relative tolerance defaults to zero and is limited to [0, 1]. It is relative
    to abs(expected), or the largest absolute supplied range endpoint. Effective
    tolerance is max(abs_tolerance, relative_tolerance * reference).
    """
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= MAX_ASSERTIONS:
        raise DesignValidationError("INVALID_ARGUMENT", "assertions must contain 1..128 explicit checks.")
    normalized, ids = [], set()
    for index, item in enumerate(assertions):
        if not isinstance(item, dict):
            raise DesignValidationError("INVALID_ARGUMENT", f"assertions[{index}] must be an object.")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            raise DesignValidationError("INVALID_ARGUMENT", f"assertions[{index}].kind is unsupported.")
        extra = ({"name"} if kind == "dimension" else {"axis"} if kind == "bbox_size" else
                 {"component_a", "component_b"} if kind == "distance" else set())
        allowed = {"id", "kind", "unit", "expected", "min", "max", "abs_tolerance", "rel_tolerance"} | extra
        if set(item) - allowed:
            raise DesignValidationError("INVALID_ARGUMENT", f"assertions[{index}] has unknown fields.")
        entry = deepcopy(item)
        label = _text(entry.get("id"), f"assertions[{index}].id", 128)
        if label in ids:
            raise DesignValidationError("INVALID_ARGUMENT", "Assertion ids must be unique.")
        ids.add(label)
        unit = entry.get("unit")
        if (kind == "dimension" and unit not in ("mm", "deg")) or (kind != "dimension" and unit != UNITS[kind]):
            raise DesignValidationError("INVALID_ARGUMENT", f"Assertion {label!r} has an incompatible unit.")
        if kind == "dimension":
            _text(entry.get("name"), f"{label}.name")
        elif kind == "bbox_size" and entry.get("axis") not in ("x", "y", "z"):
            raise DesignValidationError("INVALID_ARGUMENT", f"{label}.axis must be x, y or z.")
        elif kind == "distance":
            for field in ("component_a", "component_b"):
                name = _text(entry.get(field), f"{label}.{field}", 256)
                if "/" in name or "\\" in name:
                    raise DesignValidationError("INVALID_ARGUMENT", "Distance checks require exact top-level instance names.")
            if entry["component_a"] == entry["component_b"]:
                raise DesignValidationError("INVALID_ARGUMENT", "Distance checks require two different components.")
        expected, ranged = "expected" in entry, "min" in entry or "max" in entry
        if expected == ranged:
            raise DesignValidationError("INVALID_ARGUMENT", "Supply expected OR at least one of min/max.")
        for field in ("expected", "min", "max"):
            if field in entry:
                entry[field] = _number(entry[field], f"{label}.{field}", nonnegative=kind != "dimension")
        if "min" in entry and "max" in entry and entry["min"] > entry["max"]:
            raise DesignValidationError("INVALID_ARGUMENT", f"{label}.min cannot exceed max.")
        entry["abs_tolerance"] = _number(entry.get("abs_tolerance"), f"{label}.abs_tolerance", nonnegative=True)
        entry["rel_tolerance"] = _number(entry.get("rel_tolerance", 0), f"{label}.rel_tolerance", nonnegative=True)
        if entry["rel_tolerance"] > 1:
            raise DesignValidationError("INVALID_ARGUMENT", "rel_tolerance must be in [0, 1].")
        normalized.append(entry)
    return normalized


def _native_number(value: Any, label: str, *, nonnegative: bool = False) -> float:
    try:
        return _number(value, label, nonnegative=nonnegative)
    except DesignValidationError:
        raise DesignValidationError("NATIVE_MEASUREMENT_INVALID", f"Native {label} is not a valid finite number.") from None


def _array(value: Any, length: int, label: str) -> list[float]:
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise DesignValidationError("NATIVE_MEASUREMENT_INVALID", f"Native {label} must contain {length} numbers.")
    return [_native_number(item, label) for item in value]


def _items(value: Any, label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, (tuple, list)) or len(value) > MAX_NATIVE_ITEMS or any(item is None for item in value):
        raise DesignValidationError("NATIVE_MEASUREMENT_INVALID", f"Native {label} inventory is invalid or oversized.")
    return list(value)


def _path(value: Any) -> Path:
    _text(str(value) if isinstance(value, Path) else value, "document_path", 4096)
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() not in (".sldprt", ".sldasm") or not path.is_file()
            or path.stat().st_size == 0):
        raise DesignValidationError("INVALID_ARGUMENT", "Expected an existing absolute nonempty local .sldprt or .sldasm.")
    return path.resolve()


def _active(sw_app: Any, path: Path) -> Any:
    if sw_app is None:
        raise DesignValidationError("NOT_CONNECTED", "No attached SOLIDWORKS application; validation does not connect.")
    model = _typed(sw_app.ActiveDoc, "IModelDoc2")
    if model is None:
        raise DesignValidationError("NO_ACTIVE_DOCUMENT", "The requested saved document must already be active.")
    try:
        same = os.path.samefile(model.GetPathName(), path)
    except (OSError, ValueError, TypeError):
        same = False
    if not same:
        raise DesignValidationError("DOCUMENT_MISMATCH", "The exact requested saved document must already be active.")
    if int(model.GetType()) not in (1, 2):
        raise DesignValidationError("MODEL_REQUIRED", "Design assertions require a part or assembly.")
    return model


def _metadata(model: Any, path: Path) -> dict:
    configuration = _typed(model.GetActiveConfiguration(), "IConfiguration")
    if configuration is None:
        raise DesignValidationError("NATIVE_STATE_UNAVAILABLE", "Native active configuration is unavailable.")
    info = path.stat()
    return {"document_path": str(path), "type": int(model.GetType()), "configuration": str(configuration.Name),
            "update_stamp": int(model.GetUpdateStamp()), "dirty": bool(model.GetSaveFlag()),
            "editing_self": bool(model.IsEditingSelf()), "sketch_open": model.GetActiveSketch2() is not None,
            "file": {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
                     "device": info.st_dev, "inode": info.st_ino}}


class _Reader:
    def __init__(self, model: Any):
        self.model, self.cache, self.measurements = model, {}, {}

    def cached(self, key, function):
        if key not in self.cache:
            try:
                self.cache[key] = function()
            except Exception as exc:
                self.cache[key] = exc
        value = self.cache[key]
        if isinstance(value, Exception):
            raise value
        return value

    def dimension(self, name: str) -> tuple[float, str, dict]:
        dim = _typed(self.model.Parameter(name), "IDimension")
        if dim is None:
            raise DesignValidationError("DIMENSION_NOT_FOUND", f"Exact dimension {name!r} was not found.")
        native_name = str(dim.GetNameForSelection())
        if native_name != name:
            raise DesignValidationError("DIMENSION_NAME_MISMATCH", f"Native dimension name is {native_name!r}; use its exact name.")
        kind = int(dim.GetType())
        # swDimensionParamType_e from the installed interop: linear=0, angular=1;
        # integer=2 and unknown=-1 must never be treated as linear dimensions.
        if kind not in (0, 1):
            raise DesignValidationError("DIMENSION_TYPE_UNSUPPORTED", f"Dimension {name!r} has unsupported native type {kind}.")
        unit = "mm" if kind == 0 else "deg"
        raw = _native_number(dim.SystemValue, "dimension SystemValue")
        value = _native_number(raw * 1000 if kind == 0 else math.degrees(raw), "converted dimension")
        evidence = {"source": "IDimension.SystemValue", "name": native_name, "native_type": kind,
                    "system_value": raw, "system_unit": "m" if kind == 0 else "rad"}
        self.measurements.setdefault("dimensions", {})[name] = {"value": value, "unit": unit}
        return value, unit, evidence

    def assembly_parts(self) -> list[Any]:
        assembly = _typed(self.model, "IAssemblyDoc")
        parts, seen = [], set()
        for raw in _items(assembly.GetComponents(False), "assembly components"):
            component = _typed(raw, "IComponent2")
            name = str(component.Name2)
            if name in seen:
                raise DesignValidationError("COMPONENT_NOT_UNIQUE", f"Duplicate component {name!r}.")
            seen.add(name)
            state = int(component.GetSuppression2())
            if state == 0:
                continue
            if state not in (2, 3):
                raise DesignValidationError("COMPONENT_NOT_RESOLVED", f"Component {name!r} is not fully resolved.")
            child = _typed(component.GetModelDoc2(), "IModelDoc2")
            if child is None:
                raise DesignValidationError("COMPONENT_NOT_LOADED", f"Component {name!r} has no loaded model.")
            if int(child.GetType()) == 1:
                parts.append(component)
            elif int(child.GetType()) != 2:
                raise DesignValidationError("COMPONENT_TYPE_UNSUPPORTED", f"Component {name!r} is not a part or assembly.")
        return parts

    def mass(self) -> dict:
        if int(self.model.GetType()) == 2:
            self.cached("assembly_parts", self.assembly_parts)
        extension = _typed(self.model.Extension, "IModelDocExtension")
        mass = _typed(extension.CreateMassProperty(), "IMassProperty")
        if mass is None:
            raise DesignValidationError("MASS_UNAVAILABLE", "Native mass properties are unavailable.")
        mass.UseSystemUnits = True
        if not mass.UseSystemUnits:
            raise DesignValidationError("MASS_UNIT_UNVERIFIED", "Native mass property SI units could not be verified.")
        values = {"mass_kg": _native_number(mass.Mass, "mass", nonnegative=True),
                  "volume_mm3": _native_number(_native_number(mass.Volume, "volume", nonnegative=True) * 1e9,
                                               "converted volume", nonnegative=True),
                  "density_kg_m3": _native_number(mass.Density, "density", nonnegative=True)}
        if values["volume_mm3"] <= 0:
            raise DesignValidationError("NO_SOLID_GEOMETRY", "Native mass properties contain no positive solid volume.")
        self.measurements.update(values)
        return values

    @staticmethod
    def body_bounds(body, rotation=None, shift=None) -> tuple[list[float], list[float]]:
        rotation = rotation or [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]
        shift = shift or [0., 0., 0.]
        low, high = [], []
        for axis in range(3):
            extremes = []
            for sign in (-1, 1):
                result = body.GetExtremePoint(*[sign * value for value in rotation[axis]])
                if isinstance(result, (tuple, list)) and len(result) == 4:
                    if result[0] is not True and not (type(result[0]) is int and result[0] == 1):
                        raise DesignValidationError("BBOX_UNAVAILABLE", "Native GetExtremePoint reported failure.")
                    result = result[1:]
                point = _array(result, 3, "extreme point")
                coordinate = sum(a * b for a, b in zip(rotation[axis], point)) + shift[axis]
                extremes.append(_native_number(coordinate * 1000, "extreme point in mm"))
            low.append(min(extremes))
            high.append(max(extremes))
        return low, high

    def bounds(self) -> dict:
        boxes = []
        if int(self.model.GetType()) == 1:
            part = _typed(self.model, "IPartDoc")
            boxes = [self.body_bounds(_typed(body, "IBody2")) for body in
                     _items(part.GetBodies2(0, False), "part solid bodies")]
        else:
            for component in self.cached("assembly_parts", self.assembly_parts):
                transform = _typed(component.Transform2, "IMathTransform")
                if transform is None:
                    raise DesignValidationError("BBOX_UNAVAILABLE", "Component transform is unavailable.")
                data = _array(transform.ArrayData, 16, "component transform")
                if not _rigid(data):
                    raise DesignValidationError("COMPONENT_TRANSFORM_UNSUPPORTED", "Bounding boxes require rigid unit-scale transforms.")
                rotation = [[data[column * 3 + row] for column in range(3)] for row in range(3)]
                boxes.extend(self.body_bounds(_typed(body, "IBody2"), rotation, data[9:12]) for body in
                             _items(component.GetBodies2(0), "component solid bodies"))
        if not boxes:
            raise DesignValidationError("NO_SOLID_GEOMETRY", "No loaded solid bodies were available for bounding-box measurement.")
        low = [min(box[0][axis] for box in boxes) for axis in range(3)]
        high = [max(box[1][axis] for box in boxes) for axis in range(3)]
        size = [_native_number(b - a, "bounding-box size", nonnegative=True) for a, b in zip(low, high)]
        value = {"min_mm": low, "max_mm": high, "size_mm": size}
        self.measurements["bbox_size_mm"] = size
        self.measurements["bounding_box_mm"] = value
        return value

    def distance(self, name_a: str, name_b: str) -> float:
        if int(self.model.GetType()) != 2:
            raise DesignValidationError("ASSEMBLY_REQUIRED", "Component-distance checks require an assembly.")
        assembly = _typed(self.model, "IAssemblyDoc")
        components = {}
        for raw in _items(assembly.GetComponents(True), "top-level components"):
            component = _typed(raw, "IComponent2")
            name = str(component.Name2)
            if name in components:
                raise DesignValidationError("COMPONENT_NOT_UNIQUE", f"Duplicate top-level component {name!r}.")
            components[name] = component
        pair = []
        for name in (name_a, name_b):
            component = components.get(name)
            if component is None:
                raise DesignValidationError("COMPONENT_NOT_FOUND", f"Exact top-level component {name!r} was not found.")
            if int(component.GetSuppression2()) not in (2, 3):
                raise DesignValidationError("COMPONENT_NOT_RESOLVED", f"Component {name!r} is not fully resolved.")
            child = _typed(component.GetModelDoc2(), "IModelDoc2")
            if child is None or int(child.GetType()) != 1:
                raise DesignValidationError("DISTANCE_SCOPE_UNSUPPORTED", "Distance checks support loaded top-level part pairs only.")
            if not _items(component.GetBodies2(0), "distance solid bodies"):
                raise DesignValidationError("NO_SOLID_GEOMETRY", f"Component {name!r} has no solid bodies.")
            pair.append(component)
        extension = _typed(self.model.Extension, "IModelDocExtension")
        measure = _typed(extension.CreateMeasure(), "IMeasure")
        if measure is None:
            raise DesignValidationError("DISTANCE_UNAVAILABLE", "Native measurement is unavailable.")
        entities: Any = pair
        if hasattr(measure, "_oleobj_"):
            import pythoncom
            import win32com.client
            entities = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, pair)
        # Explicit entities avoid clearing or temporarily replacing user selection.
        # No selection-based fallback is attempted if this native route rejects it.
        if not measure.Calculate(entities):
            raise DesignValidationError("DISTANCE_UNAVAILABLE", "Native explicit-entity pair measurement was refused.")
        value = 0.0 if measure.IsIntersect else _native_number(measure.Distance, "distance", nonnegative=True)
        return _native_number(value * 1000, "distance in mm", nonnegative=True)

    def read(self, entry: dict) -> tuple[float, str, dict]:
        kind = entry["kind"]
        if kind == "dimension":
            return self.cached((kind, entry["name"]), lambda: self.dimension(entry["name"]))
        if kind in ("mass", "volume"):
            values = self.cached("mass", self.mass)
            key = "mass_kg" if kind == "mass" else "volume_mm3"
            return values[key], UNITS[kind], {"source": "IMassProperty", "system_units_verified": True,
                                             "density_kg_m3": values["density_kg_m3"]}
        if kind == "bbox_size":
            bounds = self.cached("bounds", self.bounds)
            return bounds["size_mm"]["xyz".index(entry["axis"])], "mm", {
                "source": "IBody2.GetExtremePoint", "axes": "active_model", **bounds}
        pair = (entry["component_a"], entry["component_b"])
        value = self.cached(("distance", *sorted(pair)), lambda: self.distance(*pair))
        return value, "mm", {"source": "IMeasure.Calculate(explicit_entities)", "components": list(pair),
                             "scope": "top_level_loaded_part_pair", "zero_means": "touching_or_overlapping"}


def read_design_measurements(model: Any) -> dict:
    """Read SI-normalized mass/volume and tight body bounds for an exact model.

    Internal workflow integration API: the caller must already have checked the
    active saved model, own the CAD lease/STA, and verify its state afterwards.
    This helper never obtains/activates a document, rebuilds, saves or selects.
    Native failures raise rather than inventing defaults or a successful result.
    """
    if model is None or int(model.GetType()) not in (1, 2):
        raise DesignValidationError("MODEL_REQUIRED", "Mass and bounds require a native part or assembly.")
    reader = _Reader(model)
    reader.mass()
    reader.bounds()
    return deepcopy(reader.measurements)


def _evaluate(entry: dict, actual: float, unit: str, evidence: dict) -> dict:
    if unit != entry["unit"]:
        raise DesignValidationError("UNIT_MISMATCH", f"Native unit {unit!r} differs from requested {entry['unit']!r}.")
    endpoints = [entry[key] for key in ("expected", "min", "max") if key in entry]
    tolerance = max(entry["abs_tolerance"], entry["rel_tolerance"] * max(abs(value) for value in endpoints))
    if "expected" in entry:
        passed = abs(actual - entry["expected"]) <= tolerance
    else:
        passed = (("min" not in entry or actual >= entry["min"] or entry["min"] - actual <= tolerance)
                  and ("max" not in entry or actual <= entry["max"] or actual - entry["max"] <= tolerance))
    return {**entry, "actual": actual, "effective_tolerance": tolerance, "passed": passed,
            "status": "pass" if passed else "fail", "code": "SATISFIED" if passed else "CONDITION_FAILED",
            "evidence": evidence}


def validate_design(sw_app: Any, *, document_path: str | Path, assertions: list[dict]) -> dict:
    """Validate current native values without rebuilding or changing a document.

    Caller serializes this function on the existing COM STA under the CAD lease.
    ok=True means every requested check completed; passed additionally requires
    every condition to hold. Unknown measurements and changed document state
    return ok=False, passed=False. No result claims manufacturing correctness.
    """
    report = {"ok": False, "passed": False, "status": "unknown", "assertions": [], "errors": [],
              "measurements": {}, "document_states": {}, "mutation_attempted": False,
              "saved": False, "rebuilt": False, "limits": [
                  "Checks read the current native state; they do not rebuild or establish unlisted design requirements.",
                  "Mass uses native effective density and any native overrides; assigned engineering material is not certified.",
                  "Component distance is unsigned: zero means touching or overlapping, not an interference-free fit."]}
    try:
        entries = validate_assertions(assertions)
        path = _path(document_path)
        report["document_path"] = str(path)
        model = _active(sw_app, path)
        before = _metadata(model, path)
        report["document_states"]["before"] = before
        if not before["editing_self"] or before["sketch_open"]:
            raise DesignValidationError("DOCUMENT_EDIT_MODE", "Finish the open sketch or in-context edit before validation.")
    except Exception as exc:
        report["errors"].append({"code": getattr(exc, "code", "NATIVE_STATE_UNAVAILABLE"), "message": str(exc)})
        report["counts"] = {"total": len(assertions) if isinstance(assertions, list) else 0,
                            "passed": 0, "failed": 0, "unknown": len(assertions) if isinstance(assertions, list) else 0}
        return report
    reader = _Reader(model)
    for entry in entries:
        try:
            result = _evaluate(entry, *reader.read(entry))
        except Exception as exc:
            code = getattr(exc, "code", "MEASUREMENT_UNAVAILABLE")
            result = {**entry, "actual": None, "passed": False, "status": "unknown", "code": code,
                      "message": str(exc)}
            report["errors"].append({"id": entry["id"], "code": code, "message": str(exc)})
        report["assertions"].append(result)
    report["measurements"] = reader.measurements
    try:
        final = _metadata(_active(sw_app, path), path)
        report["document_states"]["final"] = final
        report["state_unchanged"] = final == before
        if final != before:
            raise DesignValidationError("DOCUMENT_CHANGED_DURING_VALIDATION", "Document state changed during validation; measurements are indeterminate.")
    except Exception as exc:
        report["state_unchanged"] = False
        code = getattr(exc, "code", "FINAL_STATE_UNAVAILABLE")
        report["errors"].append({"code": code, "message": str(exc)})
        for result in report["assertions"]:
            result.update(observed_status=result["status"], status="unknown", passed=False, code=code)
    counts = {"total": len(entries), "passed": 0, "failed": 0, "unknown": 0}
    for result in report["assertions"]:
        counts[{"pass": "passed", "fail": "failed", "unknown": "unknown"}[result["status"]]] += 1
    report["counts"] = counts
    report["ok"] = not report["errors"] and counts["unknown"] == 0
    report["passed"] = report["ok"] and counts["failed"] == 0
    report["status"] = "unknown" if not report["ok"] else "pass" if report["passed"] else "fail"
    return report


def assertion_schema() -> dict:
    """Shared exact assertion input schema for validation and variant studies."""
    number = {"type": "number"}
    branches = []
    for kind in sorted(KINDS):
        properties = {"id": {"type": "string", "minLength": 1, "maxLength": 128},
                      "kind": {"const": kind}, "unit": {"enum": ["mm", "deg"]} if kind == "dimension" else {"const": UNITS[kind]},
                      "expected": number, "min": number, "max": number,
                      "abs_tolerance": {"type": "number", "minimum": 0},
                      "rel_tolerance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0}}
        required = ["id", "kind", "unit", "abs_tolerance"]
        if kind == "dimension":
            properties["name"] = {"type": "string", "minLength": 1, "maxLength": 512}
            required.append("name")
        elif kind == "bbox_size":
            properties["axis"] = {"enum": ["x", "y", "z"]}
            required.append("axis")
        elif kind == "distance":
            for field in ("component_a", "component_b"):
                properties[field] = {"type": "string", "minLength": 1, "maxLength": 256}
                required.append(field)
        if kind != "dimension":
            for field in ("expected", "min", "max"):
                properties[field] = {"type": "number", "minimum": 0}
        branches.append({"type": "object", "properties": properties, "required": required,
                         "additionalProperties": False, "oneOf": [
                             {"required": ["expected"], "not": {"anyOf": [{"required": ["min"]}, {"required": ["max"]}]}},
                             {"not": {"required": ["expected"]}, "anyOf": [{"required": ["min"]}, {"required": ["max"]}]}]})
    return {"type": "array", "minItems": 1, "maxItems": MAX_ASSERTIONS, "items": {"oneOf": branches}}


def design_tools() -> list[types.Tool]:
    return [types.Tool(name="pro_validate_design", description=(
        "Check explicit dimension, mass, volume, axis-aligned size and exact top-level part-pair distance requirements "
        "on the active saved model. Requires explicit units and tolerances. Never rebuilds, saves, activates or changes selection. "
        "ok means every check completed; passed means all conditions hold. Unknown is never pass."), inputSchema={
            "type": "object", "properties": {
                "document_path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "assertions": assertion_schema(), "result_view": {"type": "string", "enum": ["summary", "full"]}},
            "required": ["document_path", "assertions"], "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))]
