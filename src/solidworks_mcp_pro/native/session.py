"""Independent, thread-owned CAD session and native mutation checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import math
import os
import threading
import time
import traceback
from types import MethodType
from uuid import uuid4

from . import binding, ENGINE_ID
from .catalogue import DOCUMENT_OPERATIONS, OFFLINE, READ_ONLY, contracts, handler
from .errors import CadError


def same_object(first, second) -> bool:
    if first is None or second is None:
        return first is second
    try:
        return getattr(first, "_oleobj_", first) == getattr(second, "_oleobj_", second)
    except Exception as exc:
        # A closed native document can leave a disconnected Python proxy.
        # Comparing IUnknown identities may itself call QueryInterface.
        if getattr(exc, "hresult", None) in (-2147417848, -2147220995, -2147023174):
            return False
        raise


def feature_walk(model):
    """Bounded native traversal, including absorbed sketches exactly once."""
    seen, stack = set(), [binding.wrap(model.FirstFeature(), "IFeature")]
    while stack:
        feature = stack.pop()
        if feature is None:
            continue
        key = (str(feature.Name), str(feature.GetTypeName2()))
        if key in seen:
            continue
        if len(seen) >= 10000:
            raise CadError("FEATURE_TRAVERSAL_LIMIT", "Native feature inventory exceeds its traversal limit.")
        seen.add(key)
        next_feature = binding.wrap(feature.GetNextFeature(), "IFeature")
        if next_feature is not None:
            stack.append(next_feature)
        child = binding.wrap(feature.GetFirstSubFeature(), "IFeature")
        siblings = set()
        while child is not None:
            child_key = (str(child.Name), str(child.GetTypeName2()))
            if child_key in siblings:
                raise CadError("FEATURE_TRAVERSAL_CYCLE", "Native subfeature inventory contains a cycle.")
            siblings.add(child_key)
            stack.append(child)
            child = binding.wrap(child.GetNextSubFeature(), "IFeature")
        yield feature


class NativeSession:
    def __init__(self, app=None):
        self._owner = threading.get_ident()
        self._sw, self._model = app, None
        self._documents_rights = []
        self._topology_cache = {}
        self.selection_tolerance_mm = 0.05
        self.last_receipt = None

    @property
    def _mod(self):
        return binding.module()

    def _check_thread(self):
        if threading.get_ident() != self._owner:
            raise CadError("WRONG_COM_THREAD", "Native CAD access must stay on its owning STA worker.")

    def attach(self, *, launch=False):
        self._check_thread()
        if self._sw is not None:
            return self._sw
        if os.name != "nt":
            if launch:
                raise CadError("NATIVE_UNAVAILABLE", "SOLIDWORKS COM requires Windows.")
            return None
        import win32com.client
        try:
            raw = win32com.client.GetActiveObject("SldWorks.Application")
        except Exception:
            if not launch:
                return None
            raw = win32com.client.Dispatch("SldWorks.Application")
        self._sw = binding.wrap(raw, "ISldWorks")
        return self._sw

    def _reattach_if_gone(self):
        self._check_thread()
        if self._sw is not None:
            try:
                self._sw.RevisionNumber()
                return
            except Exception as exc:
                # Do not reinterpret a busy/hung COM application as a license to
                # start another instance. Only a disconnected object is retired.
                code = getattr(exc, "hresult", None)
                if code not in (-2147417848, -2147220995, -2147023174):
                    raise
                self._sw, self._model = None, None
                self._documents_rights.clear()
                self._topology_cache.clear()
        self.attach()

    def _ensure(self):
        self._check_thread()
        if self._sw is None:
            self.attach()
        if self._sw is None:
            raise CadError("NOT_CONNECTED", "Start SOLIDWORKS, then call get_status to attach to it.")
        return self._sw

    def _open_documents(self):
        app = self._ensure()
        raw = app.GetDocuments() or ()
        if not isinstance(raw, (tuple, list)) or len(raw) > 10000:
            raise CadError("DOCUMENT_INVENTORY_INVALID", "Native document inventory is unavailable or oversized.")
        return [binding.wrap(document, "IModelDoc2") for document in raw]

    def remember(self, model, *, editable=False, opened=False):
        self._check_thread()
        for record in self._documents_rights:
            if same_object(record["model"], model):
                record.update(editable=record["editable"] or editable, opened=record["opened"] or opened)
                return
        self._documents_rights.append({"model": model, "editable": editable, "opened": opened})

    def rights(self, model):
        self._check_thread()
        return next((record for record in self._documents_rights if same_object(record["model"], model)),
                    {"editable": False, "opened": False})

    def forget(self, model):
        """Retire ownership before CloseDoc can invalidate its COM proxies."""
        self._check_thread()
        self._documents_rights = [record for record in self._documents_rights
                                 if not same_object(record["model"], model)]

    def close_document(self, model):
        """Close one document without SOLIDWORKS retiring other hidden models."""
        self._check_thread()
        app = self._ensure()
        title, path = str(model.GetTitle()), str(model.GetPathName())
        others = [doc for doc in self._open_documents() if not same_object(doc, model)]
        visibility = [(doc, bool(doc.Visible)) for doc in others]
        app_visible = getattr(app, "Visible", None)
        original_rights = dict(self.rights(model))
        was_target = same_object(model, self._model)
        changed, failure, restoration_errors, forgotten = [], None, [], False
        try:
            # CloseDoc also closes *all* non-active hidden documents. Protect
            # their UI lifetime first, regardless of this session's edit rights.
            for doc, visible in visibility:
                if not visible:
                    changed.append(doc)
                    doc.Visible = True
                    if not bool(doc.Visible):
                        raise CadError("DOCUMENT_VISIBILITY_UNVERIFIED", "Cannot protect another hidden document before closing.")
            self.forget(model)
            forgotten = True
            app.CloseDoc(title)
            remaining = self._open_documents()
            if any(str(doc.GetTitle()) == title and str(doc.GetPathName()) == path for doc in remaining):
                raise CadError("DOCUMENT_CLOSE_UNVERIFIED", "The requested document remains open.")
            if any(not any(same_object(doc, current) for current in remaining) for doc in others):
                raise CadError("FOREIGN_DOCUMENT_CLOSED", "Another native document disappeared during CloseDoc.",
                               outcome_may_be_indeterminate=True)
            if was_target:
                self._model = None
        except Exception as exc:
            failure = exc
            if forgotten and (original_rights["editable"] or original_rights["opened"]):
                try:
                    survivor = next((doc for doc in self._open_documents() if same_object(doc, model)), None)
                    if survivor is not None:
                        self.remember(survivor, editable=original_rights["editable"], opened=original_rights["opened"])
                except Exception:
                    pass
        finally:
            for doc in reversed(changed):
                try:
                    doc.Visible = False
                    if bool(doc.Visible):
                        raise RuntimeError("native visibility readback mismatch")
                except Exception as exc:
                    restoration_errors.append(str(exc))
            if app_visible is not None:
                try:
                    app.Visible = app_visible
                    if bool(app.Visible) != bool(app_visible):
                        raise RuntimeError(f"native application visibility expected {bool(app_visible)!r}, actual {bool(app.Visible)!r}")
                except Exception as exc:
                    restoration_errors.append(str(exc))
            if restoration_errors:
                raise CadError("DOCUMENT_VISIBILITY_RESTORATION_FAILED", "; ".join(restoration_errors),
                               failure=str(failure) if failure else None, outcome_may_be_indeterminate=True)
        if failure:
            raise failure
        return {"closed": title, "other_documents_preserved": len(others), "visibility_restored": True}

    def _require_model(self):
        app = self._ensure()
        active = binding.wrap(app.ActiveDoc, "IModelDoc2")
        if active is None:
            raise CadError("NO_ACTIVE_DOCUMENT", "An active part, assembly or drawing is required.")
        if self._model is None:
            self._model = active
        elif not same_object(self._model, active):
            raise CadError("ACTIVE_DOCUMENT_CHANGED", "The active CAD document changed; explicitly activate the intended target.")
        return self._model

    def _require_part(self):
        model = self._require_model()
        if int(model.GetType()) != 1:
            raise CadError("PART_REQUIRED", "This operation requires a native part document.")
        return model

    def _require_assembly(self):
        model = self._require_model()
        if int(model.GetType()) != 2:
            raise CadError("ASSEMBLY_REQUIRED", "This operation requires a native assembly document.")
        return binding.wrap(model, "IAssemblyDoc")

    def _require_editable(self):
        model = self._require_model()
        if not self.rights(model)["editable"]:
            raise CadError("DOCUMENT_NOT_AUTHORIZED", "For an existing document, call activate_document with allow_edit=true before modeling.")
        if model.IsOpenedReadOnly() or model.IsOpenedViewOnly():
            raise CadError("DOCUMENT_READ_ONLY", "The active model is read-only or view-only.")
        if model.GetActiveSketch2() is not None:
            raise CadError("USER_SKETCH_ACTIVE", "Finish the existing sketch before starting a separate modeling operation.")
        if not model.IsEditingSelf():
            raise CadError("IN_CONTEXT_EDIT_ACTIVE", "Finish the in-context edit before changing the target document.")
        return model

    def find_feature(self, name):
        if not isinstance(name, str) or not name:
            raise CadError("INVALID_REFERENCE", "An exact nonempty feature name is required.")
        values = [feature for feature in feature_walk(self._require_model()) if str(feature.Name) == name]
        if len(values) != 1:
            raise CadError("FEATURE_NOT_UNIQUE", f"Expected one native feature named {name!r}; found {len(values)}.")
        return values[0]

    def resolve_plane(self, name="front"):
        if not isinstance(name, str) or not name:
            raise CadError("INVALID_PLANE", "A native plane alias or exact feature name is required.")
        aliases = {"front": 0, "top": 1, "right": 2}
        if name.lower() in aliases:
            model = self._require_model()
            planes = [feature for feature in feature_walk(model) if str(feature.GetTypeName2()) == "RefPlane"]
            index = aliases[name.lower()]
            if len(planes) <= index:
                raise CadError("PLANE_NOT_FOUND", "The native template does not expose three reference planes.")
            return planes[index]
        return self.find_feature(name)

    def resolve_face(self, reference, point_mm=None, component=None):
        from .topology import resolve
        return resolve(self, reference, "face", component=component, point_mm=point_mm)

    def resolve_edges(self, edges=None, **filters):
        from .topology import resolve_edges
        return resolve_edges(self, edges, **filters)

    def select(self, entity, mark=0, append=False):
        if entity is None:
            raise CadError("ENTITY_NOT_FOUND", "Cannot select a missing native entity.")
        if not any(hasattr(entity, method) for method in ("Select4", "Select2", "Select")):
            entity = binding.wrap(entity, "IEntity")
        if hasattr(entity, "Select4"):
            manager = binding.wrap(self._require_model().SelectionManager, "ISelectionMgr")
            data = binding.wrap(manager.CreateSelectData(), "ISelectData")
            data.Mark = int(mark)
            selected = entity.Select4(bool(append), data)
        elif hasattr(entity, "Select2"):
            selected = entity.Select2(bool(append), int(mark))
        else:
            selected = entity.Select(bool(append))
        if selected is False:
            raise CadError("NATIVE_SELECTION_FAILED", "SOLIDWORKS refused the requested native entity selection.")
        return True

    def _command_in_progress(self, value):
        self._check_thread()
        if self._sw is None:
            return None
        previous = bool(self._sw.CommandInProgress)
        if value is None or previous == value:
            return None
        try:
            self._sw.CommandInProgress = bool(value)
            if bool(self._sw.CommandInProgress) != bool(value):
                raise CadError("COMMAND_MODE_UNVERIFIED", "Native command mode did not retain its requested state.")
        except Exception as exc:
            restored = False
            try:
                self._sw.CommandInProgress = previous
                restored = bool(self._sw.CommandInProgress) == previous
            except Exception:
                pass
            raise CadError("COMMAND_MODE_SETUP_FAILED", str(exc), restoration_verified=restored,
                           outcome_may_be_indeterminate=not restored) from exc
        return previous

    @contextmanager
    def _quiet_ui(self):
        saved = []
        model = self._model
        try:
            if model is not None:
                for target, attribute in ((binding.wrap(model.ActiveView, "IModelView"), "EnableGraphicsUpdate"),
                                          (binding.wrap(model.FeatureManager, "IFeatureManager"), "EnableFeatureTree")):
                    if target is not None:
                        previous = bool(getattr(target, attribute))
                        saved.append((target, attribute, previous))
                        setattr(target, attribute, False)
            yield
        finally:
            errors = []
            for target, attribute, previous in reversed(saved):
                try:
                    setattr(target, attribute, previous)
                    if bool(getattr(target, attribute)) != previous:
                        raise RuntimeError("readback mismatch")
                except Exception as exc:
                    errors.append(f"{attribute}: {exc}")
            if errors:
                raise CadError("UI_RESTORATION_FAILED", "; ".join(errors), outcome_may_be_indeterminate=True)

    def _snapshot(self, model):
        if model is None:
            return None
        from ..design_validation import _Reader
        native_features = list(feature_walk(model))
        features = [(str(item.Name), str(item.GetTypeName2())) for item in native_features]
        native_errors = set()
        for item in native_features:
            code, warning = item.GetErrorCode2()
            if code and not warning:
                native_errors.add((str(item.Name), str(item.GetTypeName2()), int(code)))
        try:
            if int(model.GetType()) == 1 and not binding.wrap(model, "IPartDoc").GetBodies2(0, False):
                volume = 0.0
            else:
                volume = _Reader(model).mass()["volume_mm3"]
        except Exception:
            volume = None
        manager = binding.wrap(model.SelectionManager, "ISelectionMgr")
        count = int(manager.GetSelectedObjectCount2(-1))
        if not 0 <= count <= 10000:
            raise CadError("SELECTION_INVENTORY_INVALID", "Native selection inventory is oversized or invalid.")
        selection = [(manager.GetSelectedObject6(index, -1), int(manager.GetSelectedObjectMark(index)))
                     for index in range(1, count + 1)]
        return {"model": model, "features": features, "native_errors": native_errors, "volume_mm3": volume,
                "selection": selection, "configuration": str(model.GetActiveConfiguration().Name)}

    def _restore_selection(self, snapshot):
        if snapshot is None:
            return
        model = snapshot["model"]
        model.ClearSelection2(True)
        manager = binding.wrap(model.SelectionManager, "ISelectionMgr")
        for entity, mark in snapshot["selection"]:
            if entity is None:
                raise CadError("SELECTION_RESTORATION_FAILED", "A previously selected entity no longer exists.")
            count = manager.AddSelectionListObject(entity, None)
            if not count:
                raise CadError("SELECTION_RESTORATION_FAILED", "Native selection-list restoration was refused.")
            index = int(manager.GetSelectedObjectCount2(-1))
            retained = manager.SetSelectedObjectMark(index, mark, 0)
            if retained is False or int(manager.GetSelectedObjectMark(index)) != mark:
                raise CadError("SELECTION_RESTORATION_FAILED", "Native selection marks could not be restored.")

    def _postconditions(self, model, before):
        if not same_object(binding.wrap(self._ensure().ActiveDoc, "IModelDoc2"), model):
            raise CadError("ACTIVE_DOCUMENT_CHANGED", "The active target changed during native execution.",
                           outcome_may_be_indeterminate=True)
        errors = []
        previous_errors = before.get("native_errors", set()) if before else set()
        for feature in feature_walk(model):
            code, warning = feature.GetErrorCode2()
            if code and not warning and (str(feature.Name), str(feature.GetTypeName2()), int(code)) not in previous_errors:
                errors.append({"name": str(feature.Name), "native_code": int(code)})
        if errors:
            raise CadError("CAD_POSTCONDITION_FAILED", "New native feature errors were reported.", broken_features=errors)

    def run_guarded(self, function, *args, **kwargs):
        """Record a native undo unit and keep restoration inside STA ownership."""
        self._check_thread()
        name = getattr(function, "__name__", "native_operation")
        if name in DOCUMENT_OPERATIONS or name in READ_ONLY:
            return function(*args, **kwargs)
        model = self._require_editable()
        original_command = self._command_in_progress(True)
        snapshot, extension, recording, finished = None, None, False, False
        failure, value = None, None
        receipt = {"operation": name, "native_engine": ENGINE_ID, "undo_recorded": False,
                   "rollback": {"attempted": False}}
        try:
            snapshot = self._snapshot(model)
            extension = binding.wrap(model.Extension, "IModelDocExtension")
            extension.StartRecordingUndoObject()
            recording = True
            with self._quiet_ui():
                value = function(*args, **kwargs)
                if not isinstance(value, dict) or value.get("ok") is not True:
                    raise CadError("NATIVE_OPERATION_FAILED", "Native handler did not report verified success.", failure=value)
                self._postconditions(model, snapshot)
        except Exception as exc:
            failure = exc
        finally:
            try:
                target_retained = same_object(binding.wrap(self._sw.ActiveDoc, "IModelDoc2"), model)
            except Exception:
                target_retained = False
            receipt["active_target_retained"] = target_retained
            if not target_retained:
                failure = failure or CadError("ACTIVE_DOCUMENT_CHANGED", "The native target is no longer active.",
                                               outcome_may_be_indeterminate=True)
            if recording:
                try:
                    finished = bool(extension.FinishRecordingUndoObject2("PRO " + uuid4().hex[:12], False))
                    receipt["undo_recorded"] = finished
                    if not finished:
                        receipt["warning"] = "Native undo unit unavailable; successful native readback is not a rollback assurance."
                except Exception as exc:
                    failure = failure or CadError("UNDO_RECORD_FINISH_FAILED", str(exc), outcome_may_be_indeterminate=True)
            if failure is not None and recording and finished and target_retained:
                receipt["rollback"]["attempted"] = True
                try:
                    model.EditUndo2(1)
                    after = self._snapshot(model)
                    verified = (after["features"] == snapshot["features"] and after["configuration"] == snapshot["configuration"]
                        and after["native_errors"] == snapshot["native_errors"]
                        and snapshot["volume_mm3"] is not None and after["volume_mm3"] is not None and math.isclose(
                            after["volume_mm3"], snapshot["volume_mm3"], rel_tol=1e-9, abs_tol=1e-6))
                    receipt["rollback"].update(verified=bool(verified), scope="native undo plus feature/configuration/volume signature")
                except Exception as exc:
                    receipt["rollback"].update(verified=False, error=str(exc))
            try:
                if target_retained:
                    self._restore_selection(snapshot)
                    receipt["selection_restored"] = True
                else:
                    receipt["selection_restored"] = False
                    receipt["selection_restore_skipped"] = "Active target changed; foreign document was preserved."
            except Exception as exc:
                receipt["selection_restoration_error"] = str(exc)
                failure = failure or exc
            if original_command is not None:
                try:
                    self._command_in_progress(original_command)
                except Exception as exc:
                    receipt["command_restoration_error"] = str(exc)
                    failure = failure or exc
            self.last_receipt = receipt
            self._topology_cache = {}
        if failure is not None:
            if isinstance(failure, CadError):
                failure.details["native_receipt"] = receipt
                if not receipt["rollback"].get("verified"):
                    failure.details["outcome_may_be_indeterminate"] = True
                raise failure
            frames = [{"file": frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
                       "function": frame.name, "line": frame.lineno}
                      for frame in traceback.extract_tb(failure.__traceback__)[-6:]]
            raise CadError("NATIVE_OPERATION_FAILED", str(failure), native_receipt=receipt, native_trace=frames,
                           outcome_may_be_indeterminate=not receipt["rollback"].get("verified")) from failure
        return {**value, "native_receipt": receipt}

    def feature_result(self, feature, name=None, **extra):
        feature = binding.wrap(feature, "IFeature")
        if feature is None:
            raise CadError("FEATURE_CREATION_FAILED", "The native feature operation returned no feature.")
        if name:
            feature.Name = str(name)
        code, warning = feature.GetErrorCode2()
        if code and not warning:
            raise CadError("FEATURE_CREATION_FAILED", "The created feature reports a native rebuild error.",
                           feature=str(feature.Name), native_code=int(code))
        result = {"ok": True, "feature": str(feature.Name), "feature_type": str(feature.GetTypeName2()), **extra}
        try:
            result.update(self.get_mass_properties())
        except Exception as exc:
            if getattr(exc, "code", None) not in {"NO_SOLID_GEOMETRY", "MASS_UNAVAILABLE", "SOLID_MASS_UNAVAILABLE"}:
                raise
            result["measurement_warning"] = str(exc)
        return result

    def invoke(self, name, arguments):
        self._check_thread()
        started = time.perf_counter()
        try:
            function = getattr(self, name)
            if name in READ_ONLY or name in OFFLINE or name in DOCUMENT_OPERATIONS:
                result = function(**arguments)
            else:
                result = self.run_guarded(function, **arguments)
            if not isinstance(result, dict):
                raise CadError("NATIVE_RESULT_INVALID", "Native operations must return structured evidence.")
        except CadError as exc:
            result = exc.result()
        except Exception as exc:
            frames = [{"file": frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
                       "function": frame.name, "line": frame.lineno}
                      for frame in traceback.extract_tb(exc.__traceback__)[-6:]]
            result = {"ok": False, "code": "NATIVE_API_ERROR", "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                      "native_trace": frames}
        return {**result, "native_engine": ENGINE_ID, "native_duration_ms": round((time.perf_counter() - started) * 1000, 3)}

    def __getattr__(self, name):
        if name not in contracts():
            raise AttributeError(name)
        implementation = handler(name)
        @wraps(implementation)
        def method(session, *args, **kwargs):
            return implementation(session, *args, **kwargs)
        method.__name__ = name
        return MethodType(method, self)

    def dispose(self):
        self._check_thread()
        self._topology_cache.clear()
        self._documents_rights.clear()
        self._model, self._sw = None, None
