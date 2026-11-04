"""Small, isolated parameter studies on saved, self-contained native parts.

The caller owns the CAD lease and invokes this synchronous module on the native
STA. Every candidate starts from a fresh disk copy. The source document is
never changed, rebuilt, saved, or closed. There is no optimizer or
automatic change to design intent: only explicit Cartesian parameter values.
"""
from __future__ import annotations

import csv
import hashlib
from itertools import product
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from .animation_bridge import _same_file, _typed


MAX_VARIANTS = 16
MAX_PARAMETERS = 2


class VariantStudyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _number(value: Any, label: str) -> float:
    try:
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError
        return float(value)
    except (ValueError, OverflowError):
        raise VariantStudyError("INVALID_ARGUMENT", f"{label} must be a finite number, not a boolean.") from None


def normalize_study_parameters(parameters: list[dict]) -> list[dict]:
    """Validate the complete product before any file or CAD mutation."""
    if not isinstance(parameters, list) or not 1 <= len(parameters) <= MAX_PARAMETERS:
        raise VariantStudyError("INVALID_ARGUMENT", "Use one or two explicit driving dimensions.")
    result, seen, count = [], set(), 1
    for parameter in parameters:
        if not isinstance(parameter, dict) or set(parameter) != {"dimension", "unit", "values"}:
            raise VariantStudyError("INVALID_ARGUMENT", "Each parameter requires dimension, unit and values only.")
        name, unit, values = parameter["dimension"], parameter["unit"], parameter["values"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 512
                or "@" not in name or any(ord(char) < 32 for char in name) or name.casefold() in seen):
            raise VariantStudyError("INVALID_ARGUMENT", "Use unique exact dimension names, such as D1@BlockExtrude.")
        if not isinstance(unit, str) or unit not in {"mm", "deg"}:
            raise VariantStudyError("INVALID_ARGUMENT", "A parameter unit must be mm or deg.")
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_VARIANTS:
            raise VariantStudyError("INVALID_ARGUMENT", "Each parameter needs one to sixteen explicit values.")
        normalized = [_number(value, "Parameter value") for value in values]
        if len(set(normalized)) != len(normalized):
            raise VariantStudyError("INVALID_ARGUMENT", "Duplicate values would create duplicate variants.")
        count *= len(normalized)
        if count > MAX_VARIANTS:
            raise VariantStudyError("INVALID_ARGUMENT", "The Cartesian product cannot exceed sixteen variants.")
        seen.add(name.casefold())
        result.append({"dimension": name, "unit": unit, "values": normalized})
    return result


def variant_tools():
    """Return the public contract without importing or starting a CAD backend."""
    from mcp import types
    from .design_validation import assertion_schema

    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "document_path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"},
            "parameters": {"type": "array", "minItems": 1, "maxItems": MAX_PARAMETERS, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "dimension": {"type": "string", "minLength": 3, "maxLength": 512},
                    "unit": {"type": "string", "enum": ["mm", "deg"]},
                    "values": {"type": "array", "minItems": 1, "maxItems": MAX_VARIANTS,
                               "uniqueItems": True, "items": {"type": "number"}},
                }, "required": ["dimension", "unit", "values"],
            }},
            "assertions": assertion_schema(),
            "dry_run": {"type": "boolean", "default": True},
            "result_view": {"type": "string", "enum": ["summary", "full"]},
        }, "required": ["document_path", "request_id", "parameters"],
    }
    return [types.Tool(name="pro_variant_study", description=(
        "Evaluate up to sixteen explicit parameter combinations on fresh saved part copies. "
        "Requires a self-contained saved .sldprt and a clean source if open; never edits the source. "
        "Checks rebuilds and dimension readbacks, reports mass/volume/tight bounds, optional design assertions, "
        "CSV/JSON evidence and the lightest valid candidate. dry_run defaults true and writes nothing."
    ), inputSchema=schema, annotations=types.ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))]


def _source_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise VariantStudyError("INVALID_ARGUMENT", "document_path must name an existing saved native part.")
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() != ".sldprt" or not path.is_file() or not path.stat().st_size):
        raise VariantStudyError("INVALID_ARGUMENT", "Use an existing nonempty absolute local .sldprt file.")
    return path.resolve(strict=True)


def _output_directory(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise VariantStudyError("INVALID_ARGUMENT", "output_directory must be a new absolute local directory.")
    path = Path(value)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//"))
            or os.path.lexists(path) or not path.parent.is_dir()):
        raise VariantStudyError("OUTPUT_EXISTS_OR_INVALID", "Output directory must not exist and its parent must exist.")
    for parent in (path.parent, *path.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise VariantStudyError("OUTPUT_LINKED", "Linked output directory ancestors are unsupported.")
    return path.resolve()


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _documents(sw_app) -> list:
    raw = sw_app.GetDocuments() or ()
    if not isinstance(raw, (list, tuple)) or len(raw) > 10000:
        raise VariantStudyError("DOCUMENT_INVENTORY_UNKNOWN", "Cannot verify the open-document inventory.")
    return [_typed(model, "IModelDoc2") for model in raw]


def _matching(documents: list, path: Path) -> list:
    return [model for model in documents if _same_file(str(model.GetPathName()), path)]


def _identity(model) -> dict | None:
    if model is None:
        return None
    return {"title": str(model.GetTitle()), "path": str(model.GetPathName())}


def _model_state(model) -> dict:
    configuration = _typed(model.GetActiveConfiguration(), "IConfiguration")
    if configuration is None:
        raise VariantStudyError("MODEL_STATE_UNAVAILABLE", "Active configuration is unavailable.")
    return {"path": str(model.GetPathName()), "configuration": str(configuration.Name),
            "update_stamp": int(model.GetUpdateStamp()), "dirty": bool(model.GetSaveFlag()),
            "sketch_open": model.GetActiveSketch2() is not None}


def _active_is(sw_app, identity: dict | None) -> bool:
    active = _typed(sw_app.ActiveDoc, "IModelDoc2")
    if identity is None:
        return active is None
    if active is None or str(active.GetTitle()) != identity["title"]:
        return False
    actual = str(active.GetPathName())
    return _same_file(actual, identity["path"]) if identity["path"] else actual == ""


def _dimensions(model, parameters: list[dict]) -> list:
    resolved = []
    for parameter in parameters:
        dimension = _typed(model.Parameter(parameter["dimension"]), "IDimension")
        if dimension is None:
            raise VariantStudyError("DIMENSION_NOT_FOUND", f"Exact dimension {parameter['dimension']!r} is unavailable.")
        if str(dimension.GetNameForSelection()) != parameter["dimension"]:
            raise VariantStudyError("DIMENSION_NAME_MISMATCH", "Native dimension name differs from the exact requested name.")
        native_type = int(dimension.GetType())
        unit = {0: "mm", 1: "deg"}.get(native_type)
        if unit is None:
            raise VariantStudyError("DIMENSION_TYPE_UNSUPPORTED", "Only native double-linear and double-angular dimensions are supported.")
        if unit != parameter["unit"]:
            raise VariantStudyError("DIMENSION_UNIT_MISMATCH", f"Dimension {parameter['dimension']!r} does not use the declared unit.")
        if int(dimension.DrivenState) != 2:
            raise VariantStudyError("DIMENSION_NOT_DRIVING", f"Dimension {parameter['dimension']!r} is a reference/driven dimension.")
        _number(dimension.SystemValue, "Native dimension")
        resolved.append(dimension)
    return resolved


def _readback(model, parameters: list[dict], values: tuple) -> dict:
    result = {}
    for parameter, expected, dimension in zip(parameters, values, _dimensions(model, parameters)):
        native = _number(dimension.SystemValue, "Native dimension")
        actual = native * 1000.0 if parameter["unit"] == "mm" else math.degrees(native)
        tolerance = max(1e-6, abs(expected) * 1e-10)
        result[parameter["dimension"]] = {"requested": expected, "actual": actual,
                                        "unit": parameter["unit"], "abs_tolerance": tolerance}
        if not math.isfinite(actual) or abs(actual - expected) > tolerance:
            raise VariantStudyError("DIMENSION_READBACK_MISMATCH", f"Dimension {parameter['dimension']!r} did not retain the requested signed value.")
    return result


def _feature_errors(model) -> list[dict]:
    from .diagnostics import _features
    errors = []
    for feature, _ in _features(model):
        code, warning = feature.GetErrorCode2()
        if code and not warning:
            errors.append({"feature": str(feature.Name), "native_code": int(code)})
    return errors


def _measurements(model) -> dict:
    from .design_validation import read_design_measurements
    measured = read_design_measurements(model)
    if measured["mass_kg"] <= 0 or measured["volume_mm3"] <= 0:
        raise VariantStudyError("SOLID_MASS_UNAVAILABLE", "Ranking requires positive finite solid mass and volume.")
    if any(value <= 0 for value in measured["bbox_size_mm"]):
        raise VariantStudyError("SOLID_BOUNDS_UNAVAILABLE", "Solid bounds are empty or invalid.")
    return {**measured, "bbox_kind": "native_solid_extreme_points_part_axes"}


def _save_copy(model, path: Path) -> str:
    before = path.stat().st_mtime_ns, path.stat().st_size, _sha(path)
    model.SaveAs3(str(path), 0, 1)  # current version, silent; only this owned copy
    if not path.is_file() or path.is_symlink() or not path.stat().st_size or not _same_file(model.GetPathName(), path):
        raise VariantStudyError("VARIANT_SAVE_FAILED", "Saved variant identity or output file could not be verified.")
    after = path.stat().st_mtime_ns, path.stat().st_size, _sha(path)
    if before == after or bool(model.GetSaveFlag()):
        raise VariantStudyError("VARIANT_SAVE_FAILED", "Native save did not prove the candidate was written cleanly.")
    return after[2]


def _close_owned(sw_app, path: Path, original_paths: set[str]) -> bool:
    models = _matching(_documents(sw_app), path)
    if not models:
        return True
    if len(models) != 1 or os.path.normcase(str(path)) in original_paths:
        return False
    title = str(models[0].GetTitle())
    if sum(str(model.GetTitle()) == title for model in _documents(sw_app)) != 1:
        return False
    sw_app.CloseDoc(title)
    return not _matching(_documents(sw_app), path)


def _write_reports(result: dict, directory: Path, parameters: list[dict]) -> None:
    csv_path, manifest_path = directory / "variants.csv", directory / "variants.json"
    fields = ["id", "status", "valid", "mass_kg", "volume_mm3", "bbox_x_mm", "bbox_y_mm", "bbox_z_mm", "path", "error_code"]
    parameter_fields = [f"parameter[{item['dimension']}]_{item['unit']}" for item in parameters]
    with csv_path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields + parameter_fields)
        writer.writeheader()
        for variant in result["variants"]:
            measurements = variant.get("measurements", {})
            size = measurements.get("bounding_box_mm", {}).get("size_mm", [None] * 3)
            row = {"id": variant["id"], "status": variant["status"], "valid": variant["valid"],
                   "mass_kg": measurements.get("mass_kg"), "volume_mm3": measurements.get("volume_mm3"),
                   "path": variant.get("path", ""), "error_code": variant.get("error", {}).get("code", "")}
            row.update({f"bbox_{axis}_mm": size[index] for index, axis in enumerate("xyz")})
            row.update({field: variant["parameters"][parameter["dimension"]] for field, parameter in zip(parameter_fields, parameters)})
            writer.writerow(row)
    result.update(csv_path=str(csv_path), csv_sha256=_sha(csv_path), manifest_path=str(manifest_path))
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")


def run_variant_study(
    sw_app,
    *,
    document_path: str | Path,
    parameters: list[dict],
    output_directory: str | Path,
    assertions: list[dict] | None = None,
    dry_run: bool = True,
) -> dict:
    """Evaluate explicit combinations, restoring the original active document."""
    result = {"ok": False, "status": "rejected", "dry_run": dry_run, "source_modified": False,
              "variants": [], "ranked_variants": [], "cleanup": {"closed_owned_copies": [], "errors": []},
              "ranking_scope": "Only the explicit combinations that rebuild, retain requested dimensions and pass supplied assertions; native effective mass, not an optimization proof."}
    source, directory, source_hash, initial_active, original_paths = None, None, None, None, set()
    source_model, source_state = None, None
    owned_paths, created_directory, active_captured = [], False, False
    from .design_validation import validate_assertions, validate_design

    try:
        parameters = normalize_study_parameters(parameters)
        if type(dry_run) is not bool:
            raise VariantStudyError("INVALID_ARGUMENT", "dry_run must be a boolean.")
        normalized_assertions = validate_assertions(assertions) if assertions is not None else None
        source = _source_path(document_path)
        source_hash = _sha(source)
        result.update(source_path=str(source), source_sha256_before=source_hash, parameters=parameters)
        directory = _output_directory(output_directory)
        combinations = list(product(*(parameter["values"] for parameter in parameters)))
        result["planned_variant_count"] = len(combinations)
        if sw_app is None:
            raise VariantStudyError("NOT_CONNECTED", "An existing attached SOLIDWORKS instance is required; this tool never connects or launches it.")
        initial_documents = _documents(sw_app)
        initial_active = _identity(_typed(sw_app.ActiveDoc, "IModelDoc2"))
        active_captured = True
        result["original_active_document"] = initial_active
        original_paths = {os.path.normcase(str(Path(model.GetPathName()).resolve()))
                          for model in initial_documents if model.GetPathName()}
        open_sources = _matching(initial_documents, source)
        if len(open_sources) > 1:
            raise VariantStudyError("SOURCE_AMBIGUOUS", "Source matches multiple open documents.")
        if open_sources:
            model = open_sources[0]
            if int(model.GetType()) != 1:
                raise VariantStudyError("PART_REQUIRED", "The source must be a native part.")
            if bool(model.GetSaveFlag()):
                raise VariantStudyError("SOURCE_UNSAVED", "Save the source explicitly before studying disk copies; this tool never saves it.")
            if model.GetActiveSketch2() is not None:
                raise VariantStudyError("DOCUMENT_EDIT_MODE", "Finish the source sketch before studying variants.")
            source_model, source_state = model, _model_state(model)
            result["source_document_state_before"] = source_state
            _dimensions(model, parameters)
            result["source_dimensions_preflight"] = "verified_on_open_source"
        else:
            result["source_dimensions_preflight"] = "deferred_until_owned_copy_is_opened"
        original_model = _typed(sw_app.ActiveDoc, "IModelDoc2")
        if original_model is not None and (original_model.GetActiveSketch2() is not None
                or (int(original_model.GetType()) == 2 and not original_model.IsEditingSelf())):
            raise VariantStudyError("DOCUMENT_EDIT_MODE", "Finish the current sketch or in-context edit before opening candidate copies.")
        dependencies = sw_app.GetDocumentDependencies2(str(source), True, False, False)
        if dependencies is not None and not isinstance(dependencies, (list, tuple)):
            raise VariantStudyError("SOURCE_REFERENCES_UNKNOWN", "Source dependency isolation could not be checked.")
        if dependencies:
            raise VariantStudyError("SOURCE_REFERENCES_UNSUPPORTED", "This MVP accepts self-contained parts only; external CAD references require a packaged study.")
        result["source_reference_isolation_verified"] = True
        if dry_run:
            result.update(ok=True, status="validated", mutation_attempted=False,
                planned_variants=[{"id": f"variant_{index:03d}", "parameters": {
                    parameter["dimension"]: value for parameter, value in zip(parameters, values)
                }} for index, values in enumerate(combinations, 1)])
        else:
            directory.mkdir(exist_ok=False)
            created_directory = True
            result.update(status="running", mutation_attempted=True, output_directory=str(directory))
            study_id = uuid4().hex[:12]
            for index, values in enumerate(combinations, 1):
                variant_id = f"variant_{index:03d}"
                copy_path = directory / f"{variant_id}_{study_id}.sldprt"
                row = {"id": variant_id, "parameters": {parameter["dimension"]: value for parameter, value in zip(parameters, values)},
                       "status": "invalid", "valid": False, "saved": False, "copy_path": str(copy_path)}
                result["variants"].append(row)
                try:
                    if _sha(source) != source_hash:
                        raise VariantStudyError("SOURCE_CHANGED", "Source file changed before creating a candidate.")
                    with source.open("rb") as source_stream, copy_path.open("xb") as destination:
                        shutil.copyfileobj(source_stream, destination)
                    owned_paths.append(copy_path)
                    if _same_file(source, copy_path) or _sha(copy_path) != source_hash:
                        raise VariantStudyError("SOURCE_COPY_UNVERIFIED", "Candidate is not a verified independent source copy.")
                    opened = sw_app.OpenDoc6(str(copy_path), 1, 1, "", 0, 0)
                    raw_model = opened[0] if isinstance(opened, (tuple, list)) else opened
                    model = _typed(raw_model, "IModelDoc2")
                    if model is None or not _same_file(str(model.GetPathName()), copy_path) or int(model.GetType()) != 1:
                        raise VariantStudyError("COPY_DOCUMENT_MISMATCH", "SOLIDWORKS did not return the exact owned part copy.")
                    if isinstance(opened, (tuple, list)) and len(opened) > 1 and opened[1]:
                        raise VariantStudyError("COPY_OPEN_ERROR", "Native part-open reported an error.")
                    if not _active_is(sw_app, _identity(model)):
                        sw_app.ActivateDoc3(str(model.GetTitle()), False, 1, 0)
                        if not _active_is(sw_app, _identity(model)):
                            raise VariantStudyError("COPY_ACTIVATION_FAILED", "Owned part copy did not become active.")
                    if model.IsOpenedReadOnly() or model.IsOpenedViewOnly():
                        raise VariantStudyError("COPY_NOT_WRITABLE", "Owned part copy is not writable.")
                    row["configuration"] = _model_state(model)["configuration"]
                    dimensions = _dimensions(model, parameters)
                    for parameter, value, dimension in zip(parameters, values, dimensions):
                        dimension.SystemValue = value / 1000.0 if parameter["unit"] == "mm" else math.radians(value)
                    row["rebuild_ok"] = bool(model.ForceRebuild3(False))
                    if not row["rebuild_ok"]:
                        raise VariantStudyError("VARIANT_REBUILD_FAILED", "Candidate rebuild failed.")
                    row["dimension_readback"] = _readback(model, parameters, values)
                    row["feature_errors"] = _feature_errors(model)
                    if row["feature_errors"]:
                        raise VariantStudyError("VARIANT_FEATURE_ERRORS", "Candidate contains failed native features.")
                    row["sha256"] = _save_copy(model, copy_path)
                    row.update(saved=True, path=str(copy_path))
                    row["dimension_readback"] = _readback(model, parameters, values)
                    before_measurement = _model_state(model)
                    if before_measurement["configuration"] != row["configuration"]:
                        raise VariantStudyError("VARIANT_STATE_CHANGED", "Candidate configuration changed during parameter edits.")
                    row["measurements"] = _measurements(model)
                    row["validation"] = validate_design(sw_app, document_path=str(copy_path), assertions=normalized_assertions) if normalized_assertions else {
                        "ok": True, "passed": True, "status": "not_requested"}
                    validation = row["validation"]
                    if _model_state(model) != before_measurement or not _active_is(sw_app, _identity(model)):
                        raise VariantStudyError("VARIANT_STATE_CHANGED", "Candidate state changed during measurement or assertion checks.")
                    if validation.get("ok") is not True or validation.get("passed") is not True:
                        row.update(status="unknown" if validation.get("status") == "unknown" else "invalid",
                            error={"code": "VARIANT_ASSERTIONS_FAILED", "message": "Candidate did not satisfy every requested design assertion."})
                    else:
                        row.update(valid=True, status="valid")
                except Exception as exc:
                    code = getattr(exc, "code", "VARIANT_NATIVE_ERROR")
                    if code in {"VARIANT_STATE_CHANGED", "COPY_DOCUMENT_MISMATCH", "MODEL_STATE_UNAVAILABLE"}:
                        row["status"] = "unknown"
                    row["error"] = {"code": code, "message": str(exc)[:500]}
                finally:
                    if copy_path in owned_paths:
                        try:
                            if not _close_owned(sw_app, copy_path, original_paths):
                                raise RuntimeError("Owned copy remains open or could not be uniquely identified.")
                            result["cleanup"]["closed_owned_copies"].append(str(copy_path))
                        except Exception as exc:
                            result["cleanup"]["errors"].append({"code": "COPY_CLOSE_UNVERIFIED", "message": str(exc)[:500], "path": str(copy_path)})
                if result["cleanup"]["errors"] or row.get("error", {}).get("code") == "SOURCE_CHANGED":
                    break
            result.update(ok=True, status="completed")
    except Exception as exc:
        result.update(ok=False, status="failed" if created_directory else "rejected",
                      error={"code": getattr(exc, "code", "VARIANT_STUDY_FAILED"), "message": str(exc)[:500]})
    finally:
        if source is not None and source_hash is not None:
            try:
                result["source_sha256_after"] = _sha(source)
                result["source_unchanged"] = result["source_sha256_after"] == source_hash
                result["source_modified"] = not result["source_unchanged"]
            except Exception as exc:
                result.update(source_unchanged=False, source_modified=None)
                result["cleanup"]["errors"].append({"code": "SOURCE_HASH_UNVERIFIED", "message": str(exc)[:500]})
        if source_model is not None and source_state is not None:
            try:
                result["source_document_state_after"] = _model_state(source_model)
                result["source_document_unchanged"] = result["source_document_state_after"] == source_state
                if not result["source_document_unchanged"]:
                    raise RuntimeError("Originally open source document state changed during the study.")
            except Exception as exc:
                result["source_document_unchanged"] = False
                result["cleanup"]["errors"].append({"code": "SOURCE_DOCUMENT_CHANGED", "message": str(exc)[:500]})
        if active_captured:
            try:
                if not _active_is(sw_app, initial_active) and initial_active is not None:
                    sw_app.ActivateDoc3(initial_active["title"], False, 1, 0)
                result["active_document_restored"] = _active_is(sw_app, initial_active)
                if not result["active_document_restored"]:
                    raise RuntimeError("Original active document identity could not be restored.")
            except Exception as exc:
                result["active_document_restored"] = False
                result["cleanup"]["errors"].append({"code": "ACTIVE_DOCUMENT_RESTORE_FAILED", "message": str(exc)[:500]})
        if result.get("source_unchanged") is False or result["cleanup"]["errors"]:
            result.update(ok=False, status="indeterminate")
        variants = result["variants"]
        result["counts"] = {"planned": result.get("planned_variant_count", 0), "evaluated": len(variants),
            "valid": sum(row["valid"] for row in variants),
            "invalid": sum(row["status"] == "invalid" for row in variants),
            "unknown": sum(row["status"] == "unknown" for row in variants)}
        if result["status"] == "completed":
            ranked = sorted((row for row in variants if row["valid"]), key=lambda row: (row["measurements"]["mass_kg"], row["id"]))
            result["ranked_variants"] = [{"rank": rank, "id": row["id"], "mass_kg": row["measurements"]["mass_kg"], "path": row["path"]}
                                         for rank, row in enumerate(ranked, 1)]
            result["best_variant"] = result["ranked_variants"][0] if ranked else None
        if created_directory:
            try:
                _write_reports(result, directory, parameters)
            except Exception as exc:
                result.update(ok=False, status="failed", report_error={"code": "STUDY_REPORT_WRITE_FAILED", "message": str(exc)[:500]})
    return result
