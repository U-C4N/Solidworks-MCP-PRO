"""Read native health and apply detached, explicitly requested repair plans.

All entry points belong on the existing COM STA under the application's CAD
lease. Inspection never calls ``_require_model``: that helper can activate a
different document or exit in-context assembly editing. Plans are process-local
and never retry a native operation whose outcome might be indeterminate.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid

from .animation_bridge import _same_file, _typed

LIGHTWEIGHT = {1, 4}
RESOLVED = {2, 3}
MAX_ITEMS = 10000


class DiagnosticError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _issue(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **extra}


def _fingerprint(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _path(document_path: str | Path) -> Path:
    if not isinstance(document_path, (str, Path)) or not str(document_path) or "\x00" in str(document_path):
        raise DiagnosticError("INVALID_ARGUMENT", "document_path must name an existing absolute saved CAD file.")
    path = Path(document_path)
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or path.is_symlink()
            or path.suffix.lower() not in {".sldprt", ".sldasm"} or not path.is_file()):
        raise DiagnosticError("INVALID_ARGUMENT", "Expected an existing absolute local .sldprt or .sldasm file.")
    return path.resolve()


def _active(session: Any, path: Path):
    app = getattr(session, "_sw", None)
    if app is None:
        raise DiagnosticError("NOT_CONNECTED", "No attached SOLIDWORKS connection; inspection does not connect automatically.")
    model = _typed(app.ActiveDoc, "IModelDoc2")
    if model is None:
        raise DiagnosticError("NO_ACTIVE_DOCUMENT", "No active saved document.")
    if not _same_file(str(model.GetPathName()), path):
        raise DiagnosticError("DOCUMENT_MISMATCH", "The exact requested saved document must already be active.")
    if int(model.GetType()) not in (1, 2):
        raise DiagnosticError("DOCUMENT_UNSUPPORTED", "Only saved parts and assemblies support this health scan.")
    return model


def _file_state(path: Path) -> dict:
    info = path.stat()
    return {"path": os.path.normcase(str(path)), "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "device": info.st_dev, "inode": info.st_ino}


def _features(model: Any):
    """Walk every native feature/subfeature once, with a bounded traversal."""
    def chain(current, next_method):
        values, traversed = [], set()
        while current is not None:
            key = (str(current.Name), str(current.GetTypeName2()))
            if key in traversed or len(traversed) >= MAX_ITEMS:
                raise RuntimeError("Native feature traversal contains a cycle or exceeds its bound.")
            traversed.add(key)
            values.append(current)
            current = _typed(getattr(current, next_method)(), "IFeature")
        return values

    todo = [(item, None) for item in reversed(chain(_typed(model.FirstFeature(), "IFeature"), "GetNextFeature"))]
    seen = set()
    while todo:
        current, parent_kind = todo.pop()
        key = (str(current.Name), str(current.GetTypeName2()))
        if key in seen:
            # Absorbed features can also appear at the top level.
            continue
        if len(seen) >= MAX_ITEMS:
            raise RuntimeError("Feature traversal exceeded its bounded inventory.")
        seen.add(key)
        yield current, parent_kind
        children = chain(_typed(current.GetFirstSubFeature(), "IFeature"), "GetNextSubFeature")
        todo.extend((item, key[1]) for item in reversed(children))


def _feature_records(model, report) -> list[dict]:
    records = []
    for feature, parent_kind in _features(model):
        name, kind = str(feature.Name), str(feature.GetTypeName2())
        code, warning = feature.GetErrorCode2()
        record = {"name": name, "type": kind, "error_code": int(code), "warning": bool(warning)}
        records.append(record)
        if code:
            target = report["warnings" if warning else "errors"]
            target.append(_issue("FEATURE_WARNING" if warning else "FEATURE_ERROR",
                                 f"Native feature reports code {code}.", target=name, native_code=int(code)))
        if kind in {"ProfileFeature", "3DProfileFeature"}:
            sketch = _typed(feature.GetSpecificFeature2(), "ISketch")
            if sketch is None:
                report["uninspected"].append(_issue("SKETCH_UNINSPECTED", "Sketch interface unavailable.", target=name))
            else:
                status = int(sketch.GetConstrainedStatus())
                record["constraint_status"] = status
                # swSketchFullyDefined=3; underdefined sketches are warnings.
                if status == 2:
                    report["warnings"].append(_issue("SKETCH_NOT_FULLY_DEFINED", "Sketch is not fully defined.",
                                                     target=name, native_status=status))
                elif status in {4, 5, 6}:
                    report["errors"].append(_issue("SKETCH_CONSTRAINT_ERROR", "Sketch is over defined or has no valid solution.",
                                                 target=name, native_status=status))
                elif status != 3:
                    report["uninspected"].append(_issue("SKETCH_STATE_UNKNOWN", "Native sketch status is not classified.",
                                                       target=name, native_status=status))
        if parent_kind == "MateGroup":
            if (code and not warning) or feature.IsSuppressed():
                continue
            mate = _typed(feature.GetSpecificFeature2(), "IMate2")
            if mate is None:
                report["uninspected"].append(_issue("MATE_UNINSPECTED", "Mate interface unavailable.", target=name))
                continue
            missing = []
            for index in range(int(mate.GetMateEntityCount())):
                entity = _typed(mate.MateEntity(index), "IMateEntity2")
                if entity is None or entity.Reference is None:
                    missing.append(index)
            record["missing_mate_references"] = missing
            if missing:
                report["errors"].append(_issue("MATE_REFERENCE_MISSING", "Mate lost a face or edge reference.", target=name))
    return records


def _equation_records(model, report) -> list[dict]:
    manager = _typed(model.GetEquationMgr(), "IEquationMgr")
    if manager is None:
        raise RuntimeError("Native equation manager is unavailable.")
    count = int(manager.GetCount())
    if not 0 <= count <= MAX_ITEMS:
        raise RuntimeError("Native equation inventory exceeds its bound.")
    records = [{"index": i, "equation": str(manager.Equation(i)), "global": bool(manager.GlobalVariable(i))}
               for i in range(count)]
    variables = {names[0].lower() for record in records if record["global"]
                 and (names := re.findall(r'"([^\"]+)"', record["equation"]))}
    for record in records:
        names = re.findall(r'"([^\"]+)"', record["equation"])
        missing = [name for name in names if name.lower() not in variables and model.Parameter(name) is None]
        record["missing_references"] = missing
        value = float(manager.Value(record["index"]))
        record["value"] = value if math.isfinite(value) else None
        native_status = int(manager.Status)
        record["native_status"] = native_status
        if missing or not math.isfinite(value) or native_status == -1:
            report["errors"].append(_issue("EQUATION_ERROR", "Equation has an invalid value, native error or missing reference.",
                                          target=str(record["index"]), missing_references=missing))
    return records


def _components(model) -> list[Any]:
    assembly = _typed(model, "IAssemblyDoc")
    values = assembly.GetComponents(False) or ()
    if not isinstance(values, (list, tuple)) or len(values) > MAX_ITEMS:
        raise RuntimeError("Unexpected or oversized native component inventory.")
    return [_typed(value, "IComponent2") for value in values]


def _component_records(model, report) -> list[dict]:
    records, names = [], set()
    for component in _components(model):
        name = str(component.Name2)
        if name in names:
            raise RuntimeError(f"Duplicate native component name: {name!r}.")
        names.add(name)
        suppression = int(component.GetSuppression2())
        path = str(component.GetPathName())
        record = {"component": name, "path": path, "suppression": suppression,
                  "configuration": str(component.ReferencedConfiguration)}
        records.append(record)
        if suppression == 0:
            continue
        if suppression in LIGHTWEIGHT:
            report["warnings"].append(_issue("COMPONENT_LIGHTWEIGHT", "Component geometry is not fully loaded.", target=name))
        elif suppression not in RESOLVED:
            report["uninspected"].append(_issue("COMPONENT_STATE_UNKNOWN", "Unknown native suppression state.", target=name))
        virtual = bool(component.IsVirtual)
        record["virtual"] = virtual
        if not virtual and (not path or not Path(path).is_file()):
            report["errors"].append(_issue("REFERENCE_MISSING", "Referenced component file is missing.", target=name, path=path))
        elif not virtual:
            record["file"] = _file_state(Path(path))
    return records


def _geometry(model, report, target_prefix="") -> None:
    part = _typed(model, "IPartDoc")
    bodies = part.GetBodies2(-1, False) or ()
    if not isinstance(bodies, (tuple, list)) or len(bodies) > MAX_ITEMS:
        raise RuntimeError("Unexpected or oversized body inventory.")
    report["counts"]["bodies_checked"] = report["counts"].get("bodies_checked", 0) + len(bodies)
    for index, raw in enumerate(bodies):
        body = _typed(raw, "IBody2")
        # Check3 is a COM property, not a callable; never invoke its result.
        faults = _typed(body.Check3, "IFaultEntity")
        if faults is not None:
            count = int(faults.Count)
            if count < 0 or count > MAX_ITEMS:
                raise RuntimeError("Unexpected native geometry fault count.")
            if count:
                report["errors"].append(_issue("BODY_GEOMETRY_ERROR", "Native body check reports geometry faults.",
                                              target=f"{target_prefix}body:{index}", fault_count=count))


def _interference(model, report, metadata_reader) -> None:
    assembly = _typed(model, "IAssemblyDoc")
    manager = _typed(assembly.InterferenceDetectionManager, "IInterferenceDetectionMgr")
    if manager is None:
        raise RuntimeError("Native interference manager unavailable.")
    settings = {key: getattr(manager, key) for key in ("TreatCoincidenceAsInterference",
                "TreatSubAssembliesAsComponents", "IncludeMultibodyPartInterferences")}
    requested = {"TreatCoincidenceAsInterference": False, "TreatSubAssembliesAsComponents": False,
                 "IncludeMultibodyPartInterferences": True}
    evidence = {"settings_before": settings, "settings_requested": requested, "settings_changed": [],
                "settings_restored": False, "done_called": False}
    report.setdefault("native_checks", {})["interference"] = evidence
    restoration_errors = []
    try:
        for key, value in requested.items():
            if settings[key] != value:
                # A no-op setter can still dirty a native document. Only set
                # options whose current values differ from the requested scan.
                evidence["settings_changed"].append(key)
                setattr(manager, key, value)
            if getattr(manager, key) != value:
                raise RuntimeError(f"Interference option {key} did not retain its requested value.")
        found = manager.GetInterferences() or ()
        report["counts"]["interferences"] = len(found)
        for raw in found:
            item = _typed(raw, "IInterference")
            names = [str(_typed(comp, "IComponent2").Name2) for comp in item.Components or ()]
            report["errors"].append(_issue("INTERFERENCE", "Native interference detection reports overlapping material.",
                                          components=names, volume_mm3=float(item.Volume) * 1e9))
    finally:
        try:
            evidence["document_before_done"] = metadata_reader()
        except Exception as exc:
            report["uninspected"].append(_issue("INTERFERENCE_STATE_UNINSPECTED", str(exc), scope="before_done"))
        try:
            manager.Done()
            evidence["done_called"] = True
        except Exception as exc:
            restoration_errors.append(str(exc))
        try:
            evidence["document_after_done"] = metadata_reader()
        except Exception as exc:
            report["uninspected"].append(_issue("INTERFERENCE_STATE_UNINSPECTED", str(exc), scope="after_done"))
        for key, previous in settings.items():
            try:
                if getattr(manager, key) != previous:
                    setattr(manager, key, previous)
                if getattr(manager, key) != previous:
                    raise RuntimeError(f"{key} readback differs.")
            except Exception as exc:
                restoration_errors.append(str(exc))
        if restoration_errors:
            report["uninspected"].append(_issue("INTERFERENCE_SETTINGS_NOT_RESTORED",
                                               "; ".join(restoration_errors)))
        evidence["settings_restored"] = not restoration_errors


def diagnose(session: Any, document_path: str | Path, *, deep: bool = False) -> dict:
    """Read the exact active saved document; ``healthy`` covers requested checks.

    Basic inspection does not force rebuild, resolve components or open files.
    Deep mode adds body Check3 and assembly interference; only temporary native
    interference-manager settings are changed, and their restoration is verified.
    """
    report = {"ok": False, "healthy": False, "document_path": str(document_path), "deep": deep,
              "mutation_attempted": False, "saved": False, "errors": [], "warnings": [],
              "uninspected": [], "counts": {}, "state": {}, "document_states": {}, "limits": [
                  "A read-only scan reports current native state; it does not rebuild or establish design intent.",
                  "Basic feature/equation checks cover the active document; deep mode checks loaded component bodies."]}
    try:
        if type(deep) is not bool:
            raise DiagnosticError("INVALID_ARGUMENT", "deep must be a boolean.")
        path = _path(document_path)
        report["document_path"] = str(path)
        model = _active(session, path)
        model_type = int(model.GetType())
    except Exception as exc:
        report["errors"].append(_issue(getattr(exc, "code", "CONNECTION_UNAVAILABLE"), str(exc)))
        report["issues"] = [{**item, "severity": "error"} for item in report["errors"]]
        return report

    def inspect(scope, fn):
        try:
            value = fn()
            report["state"][scope] = value
            return value
        except Exception as exc:
            report["uninspected"].append(_issue("CHECK_INCOMPLETE", str(exc), scope=scope))
            return None

    def metadata():
        config = _typed(model.GetActiveConfiguration(), "IConfiguration")
        return {"file": _file_state(path), "type": int(model.GetType()), "configuration": str(config.Name),
                "update_stamp": int(model.GetUpdateStamp()), "dirty": bool(model.GetSaveFlag()),
                "read_only": bool(model.IsOpenedReadOnly()), "view_only": bool(model.IsOpenedViewOnly()),
                "editing_self": bool(model.IsEditingSelf()), "sketch_open": model.GetActiveSketch2() is not None}

    document = inspect("document", metadata)
    report["document_states"]["before"] = deepcopy(document)

    def capture_document_state(stage):
        try:
            _active(session, path)
            current = metadata()
            report["document_states"][stage] = current
            return current
        except Exception as exc:
            report["uninspected"].append(_issue("FINAL_STATE_UNINSPECTED", str(exc), scope=stage))
            return None

    if document and (not document["editing_self"] or document["sketch_open"]):
        report["errors"].append(_issue("DOCUMENT_EDIT_MODE", "Finish the active sketch or in-context edit before repair/render."))
    features = inspect("features", lambda: _feature_records(model, report))
    equations = inspect("equations", lambda: _equation_records(model, report))
    report["counts"].update(features=len(features or ()), equations=len(equations or ()))
    if model_type == 2:
        components = inspect("components", lambda: _component_records(model, report))
        report["counts"]["components"] = len(components or ())
        if deep:
            capture_document_state("after_basic")
            inspect("interference", lambda: _interference(model, report, metadata))
            capture_document_state("after_interference")
            def component_geometry():
                for component in _components(model):
                    name = str(component.Name2)
                    if int(component.GetSuppression2()) == 0:
                        continue
                    child = _typed(component.GetModelDoc2(), "IModelDoc2")
                    if child is None:
                        report["uninspected"].append(_issue("GEOMETRY_UNINSPECTED", "Component model is not loaded.", target=name))
                    elif int(child.GetType()) == 1:
                        inspect(f"geometry:{name}", lambda child=child, name=name: _geometry(child, report, name + "/"))
            if components is not None:
                inspect("component_geometry", component_geometry)
            capture_document_state("after_geometry")
    elif deep:
        capture_document_state("after_basic")
        inspect("geometry", lambda: _geometry(model, report))
        capture_document_state("after_geometry")
    final_document = capture_document_state("final")
    if final_document is not None and final_document != document:
        changed = [key for key in set(final_document) | set(document or {})
                   if final_document.get(key) != (document or {}).get(key)]
        interference = report.get("native_checks", {}).get("interference", {})
        dirty_only_done_effect = (
            changed == ["dirty"] and document is not None and document["dirty"] is False
            and final_document["dirty"] is True and interference.get("done_called") is True
            and interference.get("settings_restored") is True
            and interference.get("document_before_done") == document
            and interference.get("document_after_done") == final_document
            and report["document_states"].get("after_basic") == document
        )
        if dirty_only_done_effect:
            # Native scratch evidence shows Done() can set the save flag without
            # changing the model update stamp. Do not clear that flag or hide a
            # changed stamp/configuration/file/edit state behind this exception.
            report["warnings"].append(_issue("DEEP_CHECK_MARKED_DIRTY",
                "Native interference cleanup marked the document dirty; the update stamp and all other inspected document metadata stayed unchanged."))
            report["model_state_changed"] = True
            # Fingerprint the actual post-check document; the original remains
            # available under document_states.before for audit and comparison.
            report["state"]["document"] = deepcopy(final_document)
        else:
            report["uninspected"].append(_issue("DOCUMENT_CHANGED_DURING_SCAN", "Native document changed during inspection.",
                                               changed_fields=sorted(changed)))
    report["state_fingerprint"] = _fingerprint(report["state"])
    report["ok"] = True
    report["healthy"] = not report["errors"] and not report["uninspected"]
    report["issues"] = [{**item, "severity": severity}
                        for field, severity in (("errors", "error"), ("warnings", "warning"), ("uninspected", "unknown"))
                        for item in report[field]]
    report["counts"].update(errors=len(report["errors"]), warnings=len(report["warnings"]),
                            uninspected=len(report["uninspected"]))
    return report


class RepairPlans:
    """Bounded, detached plans; native results are retained after every attempt."""

    def __init__(self, max_plans: int = 128):
        if type(max_plans) is not int or not 1 <= max_plans <= 1024:
            raise ValueError("max_plans must be 1..1024")
        self.max_plans = max_plans
        self._plans: dict[str, dict] = {}

    def plan(self, session, document_path, *, actions=None, components=None) -> dict:
        path = _path(document_path)
        default_actions = actions is None
        actions = (["reconnect"] if getattr(session, "_sw", None) is None else ["rebuild"]) if actions is None else actions
        if (type(actions) is not list or not actions or len(actions) > 3
                or any(type(action) is not str or action not in {"reconnect", "resolve", "rebuild"} for action in actions)
                or len(set(actions)) != len(actions)):
            raise DiagnosticError("INVALID_ARGUMENT", "actions must contain unique reconnect, resolve or rebuild names.")
        components = [] if components is None else components
        if (type(components) is not list or len(components) > 128
                or any(type(name) is not str or not name.strip() or "\x00" in name or len(name) > 1024 for name in components)
                or len(set(components)) != len(components)
                or (bool(components) != ("resolve" in actions))):
            raise DiagnosticError("INVALID_ARGUMENT", "resolve requires explicitly named unique components; omit them for other actions.")
        if len(self._plans) >= self.max_plans:
            raise DiagnosticError("REPAIR_PLAN_CAPACITY", "Process-local repair plan capacity reached.")
        report = diagnose(session, path)
        disconnected = not report["ok"] and any(item["code"] in {"NOT_CONNECTED", "CONNECTION_UNAVAILABLE"}
                                                for item in report["errors"])
        if disconnected:
            if default_actions:
                actions = ["reconnect"]
            if actions != ["reconnect"]:
                raise DiagnosticError("NOT_CONNECTED", "A disconnected session supports only a standalone reconnect plan.")
            preconditions = {"connected": False, "connection_identity": id(getattr(session, "_sw", None)),
                             "file": _file_state(path)}
        else:
            if not report["ok"] or "document" not in report["state"] or report["uninspected"]:
                raise DiagnosticError("REPAIR_PRECONDITIONS_INCOMPLETE", "Exact native preconditions could not be inspected.")
            if report["state"]["document"]["read_only"] or report["state"]["document"]["view_only"]:
                raise DiagnosticError("DOCUMENT_READ_ONLY", "Repair requires a writable normal document.")
            if not report["state"]["document"]["editing_self"] or report["state"]["document"]["sketch_open"]:
                raise DiagnosticError("DOCUMENT_EDIT_MODE", "Finish the current sketch or in-context edit before planning repairs.")
            preconditions = {"connected": True, "fingerprint": report["state_fingerprint"]}
            by_name = {item["component"]: item for item in report["state"].get("components", [])}
            for name in components:
                if name not in by_name or by_name[name]["suppression"] not in LIGHTWEIGHT:
                    raise DiagnosticError("COMPONENT_NOT_LIGHTWEIGHT", f"Exact component {name!r} is not an inspected lightweight instance.")
        steps = [{"action": name, **({"components": deepcopy(components)} if name == "resolve" else {})}
                 for name in ("reconnect", "resolve", "rebuild") if name in actions]
        plan_id = "repair-" + uuid.uuid4().hex
        result = {"ok": True, "status": "planned", "plan_id": plan_id, "document_path": str(path),
                  "preconditions": preconditions, "steps": steps, "diagnosis": report,
                  "expected_results": ["Exact requested document remains active.",
                      "Each named resolved instance reads back as resolved.",
                      "Post-repair diagnosis reports actual remaining errors and unknown checks."],
                  "mutation_attempted": False, "saved": False, "idempotency_scope": "current_process"}
        result["requires_apply"] = True
        self._plans[plan_id] = {"plan": deepcopy(result), "result": None}
        return deepcopy(result)

    def apply(self, session, plan_id: str) -> dict:
        if type(plan_id) is not str or plan_id not in self._plans:
            raise DiagnosticError("REPAIR_PLAN_UNKNOWN", "Unknown process-local repair plan.")
        record = self._plans[plan_id]
        if record["result"] is not None:
            return {**deepcopy(record["result"]), "replayed": True}
        plan = record["plan"]
        path = _path(plan["document_path"])
        before = plan["preconditions"]
        report = diagnose(session, path)
        disconnected = not report["ok"] and any(item["code"] in {"NOT_CONNECTED", "CONNECTION_UNAVAILABLE"}
                                                for item in report["errors"])
        current = ({"connected": False, "connection_identity": id(getattr(session, "_sw", None)),
                    "file": _file_state(path)} if disconnected else
                   {"connected": True, "fingerprint": report.get("state_fingerprint")})
        if current != before or (before["connected"] and (not report["ok"] or report["uninspected"])):
            rejected = {"ok": False, "status": "rejected", "code": "REPAIR_PLAN_STALE", "plan_id": plan_id,
                        "mutation_attempted": False, "requires_apply": False,
                        "error": "Native document state changed; create a fresh repair plan."}
            record["result"] = rejected
            return deepcopy(rejected)
        result = {"ok": False, "status": "running", "plan_id": plan_id, "document_path": str(path),
                  "mutation_attempted": False, "saved": False, "requires_apply": False,
                  "steps": [], "idempotency_scope": "current_process"}
        # Retain before invoking COM. Exceptions/timeouts cannot make this plan retryable.
        record["result"] = result
        try:
            for step in plan["steps"]:
                result["mutation_attempted"] = True
                action = step["action"]
                if action == "reconnect":
                    session._reattach_if_gone()
                    session._ensure()
                    _active(session, path)
                elif action == "resolve":
                    model = _active(session, path)
                    for name in step["components"]:
                        matches = [item for item in _components(model) if str(item.Name2) == name]
                        if len(matches) != 1 or int(matches[0].GetSuppression2()) not in LIGHTWEIGHT:
                            raise DiagnosticError("REPAIR_TARGET_CHANGED", f"Exact lightweight target changed: {name!r}.")
                        # swComponentResolved=3 resolves this instance, not its children.
                        status = int(matches[0].SetSuppression2(3))
                        if status != 2 or int(matches[0].GetSuppression2()) not in RESOLVED:
                            raise DiagnosticError("RESOLVE_FAILED", f"Native resolve/readback failed for {name!r}.")
                else:
                    if not bool(_active(session, path).ForceRebuild3(False)):
                        raise DiagnosticError("REBUILD_FAILED", "SOLIDWORKS ForceRebuild3 returned false.")
                result["steps"].append({**deepcopy(step), "ok": True})
            result["diagnosis"] = diagnose(session, path)
            result["healthy"] = result["diagnosis"]["healthy"]
            result.update(ok=result["diagnosis"]["ok"] and result["diagnosis"]["healthy"], status="completed")
            if not result["ok"]:
                result["status"] = "unresolved"
        except Exception as exc:
            result.update(status="indeterminate" if isinstance(exc, TimeoutError) else "failed",
                          code=getattr(exc, "code", "REPAIR_FAILED"), error=str(exc), changes_may_remain=True)
        return deepcopy(result)
