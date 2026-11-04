"""Owned native ICamera features; the caller holds the CAD STA and lease.

InsertCamera creates a feature. GetCameraDefinition does not add it to a model.
ICamera -> IFeature and IModelView.SetCameraByName follow the official example:
https://help.solidworks.com/2022/english/api/sldworksapi/turn_cameras_on_and_off_example_vb.htm
Perspective, FOV and aspect getters are reported only: their documented setters
are NOT implemented, despite appearing in the type library. No document save.
"""
from __future__ import annotations

import math
import re
import stat
from typing import Any

from .camera_bridge import CameraError, _array, _document_file, _number, _same_path

OWNER = "Solidworks-MCP-PRO named-camera v1"
NAME = re.compile(r"^MCP_[A-Za-z0-9_-]{1,80}$")


class NamedCameraError(CameraError):
    pass


def _module():
    import comtypes.client
    from solidworks_mcp import binding

    installed = binding.module()
    return comtypes.client.GetModule((str(installed.CLSID), installed.MajorVersion, installed.MinorVersion))


def _typed(obj, interface):
    raw = obj if hasattr(type(obj), "_iid_") else getattr(obj, "__dict__", {}).get("_comobj")
    if raw is not None and hasattr(type(raw), "_iid_"):
        if not raw:
            return None
        return raw.QueryInterface(getattr(_module(), interface))
    return obj  # Unit doubles; production enters through the typed vtable below.


def _native_context(sw_app, path):
    if not hasattr(sw_app, "_oleobj_"):
        return sw_app
    import comtypes.client

    app = comtypes.client.GetActiveObject("SldWorks.Application", _module().ISldWorks)
    if int(app.GetProcessID()) != int(sw_app.GetProcessID()):
        raise NamedCameraError("SOLIDWORKS_INSTANCE_MISMATCH", "Camera ROT connection differs from the owning CAD session.")
    model = _typed(app.ActiveDoc, "IModelDoc2")
    if model is None or not _same_path(str(model.GetPathName()), path):
        raise NamedCameraError("DOCUMENT_MISMATCH", "Camera vtable connection targets a different active document.")
    return app


def _position(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise NamedCameraError("INVALID_ARGUMENT", f"{label} requires three finite millimetre coordinates.")
    return [_number(number, label, -1e6, 1e6) for number in value]


def _point(sw_app, values_mm):
    utility = _typed(sw_app.GetMathUtility(), "IMathUtility")
    values = [value / 1000 for value in values_mm]
    if hasattr(type(utility), "_iid_"):
        from array import array
        from comtypes.automation import VARIANT

        values = VARIANT(array("d", values))
    elif hasattr(utility, "_oleobj_"):
        import pythoncom
        import win32com.client

        values = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, values)
    point = _typed(utility.CreatePoint(values), "IMathPoint")
    if point is None:
        raise RuntimeError("Native math utility could not create the target point.")
    return point


def _feature(camera):
    # A camera supports IFeature directly; the official VBA example casts it.
    # Rewrapping the same IDispatch is NOT QueryInterface. ICamera's dispatch
    # IDs collide with IFeature's, so a genuine interface query is essential.
    if hasattr(type(camera), "_iid_"):
        return camera.QueryInterface(_module().IFeature)
    if not hasattr(camera, "_oleobj_"):
        return camera
    import pythoncom
    from solidworks_mcp import binding

    cls = binding.module().IFeature
    dispatch = camera._oleobj_.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch)
    return binding.wrap(dispatch, cls)


def _deactivate(view):
    if hasattr(view, "_oleobj_"):
        import pythoncom
        import win32com.client

        view.Camera = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
    else:
        view.Camera = None


def _camera_list(extension):
    count = extension.GetCameraCount()
    if type(count) is not int or not 0 <= count <= 256:
        raise NamedCameraError("CAMERA_INVENTORY_FAILED", "Native camera count is invalid or exceeds 256.")
    result = {}
    for index in range(count):
        camera = _typed(extension.GetCameraById(index), "ICamera")
        if camera is None:
            raise NamedCameraError("CAMERA_INVENTORY_FAILED", "Native camera enumeration returned no camera.")
        name = str(_feature(camera).Name)
        if not name or name in result:
            raise NamedCameraError("CAMERA_INVENTORY_FAILED", "Native camera names are empty or duplicated.")
        result[name] = camera
    return result


def _owned(camera):
    feature = _feature(camera)
    return bool(NAME.fullmatch(str(feature.Name))) and str(feature.Description) == OWNER


def _active_name(view):
    camera = _typed(view.Camera, "ICamera")
    return str(_feature(camera).Name) if camera is not None else None


def _finite_native(value, label):
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(f"Native camera {label} is not finite.")
    return number


def _state(camera):
    target = _typed(camera.TargetPointPosition, "IMathPoint")
    position = _array(camera.GetPositionCartesian(), 3, "camera position")
    target_values = _array(target.ArrayData, 3, "camera target") if target is not None else None
    values = {"name": str(_feature(camera).Name), "id": int(camera.ID), "owned": _owned(camera),
              "type": int(camera.Type), "position_type": int(camera.PositionType),
              "position_mm": [v * 1000 for v in position],
              "target_mm": [v * 1000 for v in target_values] if target_values is not None else None,
              "roll_deg": math.degrees(_finite_native(camera.RotationRollAngle, "roll")),
              "lock_camera_position": bool(camera.LockCameraPosition),
              "target_by_selection": bool(camera.TargetPointBySelection),
              "roll_by_selection": bool(camera.RotationRollBySelection),
              "roll_flip": bool(camera.RotationRollFlipDirection),
              "perspective": bool(camera.Perspective),
              "field_of_view_height_mm": _finite_native(camera.FieldOfViewHeight, "FOV height") * 1000,
              "field_of_view_depth_mm": _finite_native(camera.FieldOfViewDepth, "FOV depth") * 1000,
              "field_of_view_angle_rad": _finite_native(camera.FieldOfViewAngle, "FOV angle"),
              "aspect_ratio": _finite_native(camera.AspectRatio, "aspect ratio")}
    return values


def _set(sw_app, camera, position_mm, target_mm, roll_deg):
    def assign(label, value):
        try:
            setattr(camera, label, value)
        except Exception as exc:
            raise RuntimeError(f"{label}: {exc}") from exc
    assign("LockCameraPosition", False)
    assign("Type", 1)  # swCameraType_AimedAtTarget, installed SW2026 + official enum.
    assign("PositionType", 1)  # swCameraPosition_Cartesian.
    assign("TargetPointBySelection", False)
    try:
        point = _point(sw_app, target_mm)
    except Exception as exc:
        raise RuntimeError(f"create_target_math_point: {exc}") from exc
    assign("TargetPointPosition", point)
    try:
        camera.SetPositionCartesian(*(number / 1000 for number in position_mm))
    except Exception as exc:
        raise RuntimeError(f"SetPositionCartesian: {exc}") from exc
    assign("RotationRollBySelection", False)
    assign("RotationRollFlipDirection", False)
    assign("RotationRollAngle", math.radians(roll_deg))


def _matches(state, position_mm, target_mm, roll_deg):
    def close(a, b):
        return math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-5)
    return (state["type"] == 1 and state["position_type"] == 1
            and state["target_by_selection"] is False and state["roll_by_selection"] is False
            and state["target_mm"] is not None
            and all(close(a, b) for a, b in zip(state["position_mm"], position_mm))
            and all(close(a, b) for a, b in zip(state["target_mm"], target_mm))
            and abs((state["roll_deg"] - roll_deg + 180) % 360 - 180) < 1e-5)


def _delete(model, extension, camera):
    model.ClearSelection2(True)
    try:
        if not _feature(camera).Select2(False, 0) or not extension.DeleteSelection2(0):
            raise RuntimeError("Native deletion of the owned camera feature was rejected.")
    finally:
        model.ClearSelection2(True)


def configure(sw_app: Any, document_path: str, *, action="inspect", name="MCP_Video",
              position_mm=None, target_mm=None, roll_deg=0, activate=True) -> dict:
    """Create/update/inspect/delete only explicitly owned MCP_ camera features."""
    path = _document_file(document_path)
    if not isinstance(action, str) or action not in {"inspect", "apply", "deactivate", "delete"}:
        raise NamedCameraError("INVALID_ARGUMENT", "Unknown named-camera action.")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise NamedCameraError("INVALID_ARGUMENT", "Camera name must match MCP_[A-Za-z0-9_-]{1,80}.")
    if type(activate) is not bool:
        raise NamedCameraError("INVALID_ARGUMENT", "activate must be a boolean.")
    roll = _number(roll_deg, "roll_deg", -3600, 3600)
    if action == "apply":
        position, target = _position(position_mm, "position_mm"), _position(target_mm, "target_mm")
        if math.dist(position, target) < 0.01:
            raise NamedCameraError("INVALID_ARGUMENT", "Camera position and target must be at least 0.01 mm apart.")
    elif position_mm is not None or target_mm is not None or roll != 0 or activate is not True:
        raise NamedCameraError("INVALID_ARGUMENT", "Position, target, roll and activate options are for apply only.")
    sw_app = _native_context(sw_app, path)
    model = _typed(sw_app.ActiveDoc, "IModelDoc2")
    if model is None or not _same_path(model.GetPathName(), path):
        raise NamedCameraError("DOCUMENT_MISMATCH", "The exact saved document must be active.")
    if model.GetType() not in (1, 2):
        raise NamedCameraError("MODEL_REQUIRED", "A part or assembly model is required.")
    if action != "inspect" and (model.IsOpenedReadOnly() or model.IsOpenedViewOnly()
                                or not path.stat().st_mode & stat.S_IWRITE):
        raise NamedCameraError("DOCUMENT_READ_ONLY", "The camera document must be writable and editable.")
    extension = _typed(model.Extension, "IModelDocExtension")
    view = _typed(model.ActiveView, "IModelView")
    if view is None:
        raise NamedCameraError("NO_ACTIVE_VIEW", "No active model view exists.")
    cameras = _camera_list(extension)
    previous_active = _active_name(view)
    base = {"document_path": str(path), "action": action, "name": name, "saved": False,
            "active_camera_before": previous_active,
            "verification_scope": "Native ICamera state only; Visualize framing requires a rendered check.",
            "read_only_optics": ["perspective", "field_of_view_height_mm", "field_of_view_depth_mm", "field_of_view_angle_rad", "aspect_ratio"]}
    if action == "inspect":
        return {**base, "ok": True, "mutation_attempted": False, "cameras": [_state(c) for c in cameras.values()]}
    camera = cameras.get(name)
    if camera is not None and not _owned(camera):
        raise NamedCameraError("CAMERA_NOT_OWNED", "An existing name is reserved by an unowned camera; it will not be changed.")
    if action in {"deactivate", "delete"} and previous_active not in (None, name):
        if action == "deactivate":
            raise NamedCameraError("OTHER_CAMERA_ACTIVE", "Refusing to deactivate a different camera.")
    if camera is None and action in {"deactivate", "delete"}:
        return {**base, "ok": True, "mutation_attempted": False, "already_absent": True,
                "active_camera_after": previous_active}
    before = _state(camera) if camera is not None else None
    if action == "apply" and before is not None and (before["type"] != 1 or before["position_type"] != 1
            or before["target_by_selection"] or before["roll_by_selection"] or before["target_mm"] is None):
        raise NamedCameraError("CAMERA_STATE_UNSUPPORTED", "Owned camera is linked/floating; exact rollback cannot be guaranteed.")
    base.update(before=before, mutation_attempted=True)
    created = False
    deleted = False
    stage = "create" if camera is None else action
    try:
        if action == "apply":
            if camera is None:
                camera = _typed(extension.InsertCamera(), "ICamera")
                if camera is None:
                    raise RuntimeError("InsertCamera returned no camera.")
                created = True
                stage = "query_feature_interface"
                feature = _feature(camera)
                stage = "set_feature_name"
                feature.Name = name
                stage = "set_feature_ownership"
                feature.Description = OWNER
                stage = "verify_feature_ownership"
                if str(feature.Name) != name or not _owned(camera):
                    raise RuntimeError("Native camera did not retain its owned name/description.")
            stage = "position_target_roll"
            _set(sw_app, camera, position, target, roll)
            stage = "activate"
            if activate and not view.SetCameraByName(name):
                raise RuntimeError("Native view refused the camera name.")
        elif action == "deactivate":
            if previous_active == name:
                _deactivate(view)
        else:
            if previous_active == name:
                _deactivate(view)
            _delete(model, extension, camera)
            deleted = True
        stage = "redraw_readback"
        model.GraphicsRedraw2()
        if not _same_path(_typed(sw_app.ActiveDoc, "IModelDoc2").GetPathName(), path):
            raise RuntimeError("Active document changed during camera mutation.")
        active_after = _active_name(view)
        if action == "apply":
            after = _state(camera)
            if not _matches(after, position, target, roll) or (activate and active_after != name):
                raise RuntimeError("Native camera readback differs from the requested state.")
        else:
            after = None if action == "delete" else _state(camera)
            if action == "delete" and name in _camera_list(extension):
                raise RuntimeError("Deleted camera remains in the native inventory.")
            if previous_active == name and active_after is not None:
                raise RuntimeError("Owned camera view was not deactivated.")
            if previous_active != name and active_after != previous_active:
                raise RuntimeError("Another active camera was changed.")
        return {**base, "ok": True, "created": created, "deleted": deleted, "after": after,
                "active_camera_after": active_after, "verified": True}
    except Exception as exc:
        errors = []
        try:
            if created and camera is not None:
                if _active_name(view) == str(_feature(camera).Name):
                    _deactivate(view)
                _delete(model, extension, camera)
                if set(_camera_list(extension)) != set(cameras):
                    raise RuntimeError("Camera inventory differs after rollback deletion.")
            elif action == "apply" and before is not None:
                _set(sw_app, camera, before["position_mm"], before["target_mm"], before["roll_deg"])
                camera.LockCameraPosition = before["lock_camera_position"]
                camera.RotationRollFlipDirection = before["roll_flip"]
                if not _matches(_state(camera), before["position_mm"], before["target_mm"], before["roll_deg"]):
                    raise RuntimeError("Camera pose rollback failed readback.")
            elif deleted:
                raise RuntimeError("Camera deletion succeeded before a later failure; deletion is not automatically rolled back.")
        except Exception as rollback_error:
            errors.append(str(rollback_error))
        try:
            if previous_active is None:
                _deactivate(view)
            elif previous_active in _camera_list(extension):
                if not view.SetCameraByName(previous_active):
                    raise RuntimeError("Previous active camera could not be restored.")
            else:
                raise RuntimeError("Previous active camera no longer exists.")
            model.GraphicsRedraw2()
            if _active_name(view) != previous_active:
                raise RuntimeError("Active camera rollback failed readback.")
        except Exception as rollback_error:
            errors.append(str(rollback_error))
        return {**base, "ok": False, "code": "NAMED_CAMERA_APPLY_FAILED", "failed_stage": stage,
                "error": str(exc), "created": created, "deleted": deleted,
                "rollback": {"attempted": True, "verified": not errors, "errors": errors},
                "outcome_may_be_indeterminate": bool(errors)}
