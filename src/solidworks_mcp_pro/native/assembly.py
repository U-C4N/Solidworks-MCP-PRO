"""Independent assembly operations and native engineering measurements.

All entry points run on the session's COM STA. Destructive operations name exact
targets; temporary measurement geometry is never inserted into the document.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from . import binding
from .errors import CadError
from .topology import component, face_records, items, number, resolve, vector, vector_pair


def _components(session, *, require_geometry=False):
    values = [
        binding.wrap(v, "IComponent2")
        for v in items(session._require_assembly().GetComponents(False), "components")
    ]
    names = [str(v.Name2) for v in values]
    if len(set(names)) != len(names):
        raise CadError(
            "COMPONENT_AMBIGUOUS", "The native component inventory contains duplicate instance names."
        )
    if require_geometry:
        for value in values:
            state = int(value.GetSuppression2())
            if state and (state not in (2, 3) or value.GetModelDoc2() is None):
                raise CadError(
                    "COMPONENT_NOT_RESOLVED", f"Component {value.Name2!r} must already be fully loaded."
                )
    return values


def _transform_data(comp):
    transform = binding.wrap(comp.Transform2, "IMathTransform")
    if transform is None:
        raise CadError("TRANSFORM_UNAVAILABLE", f"Component {comp.Name2!r} has no placement matrix.")
    return vector(transform.ArrayData, "component transform", 16)


def transform_point(values, point):
    return [
        sum(values[column * 3 + row] * point[column] for column in range(3)) + values[9 + row]
        for row in range(3)
    ]


def _native_transform(session, values):
    utility = binding.wrap(session._ensure().GetMathUtility(), "IMathUtility")
    result = binding.wrap(utility.CreateTransform(binding.array_double(values)), "IMathTransform")
    if result is None or any(
        abs(a - b) > 1e-10 for a, b in zip(vector(result.ArrayData, "created transform", 16), values)
    ):
        raise CadError(
            "TRANSFORM_CREATION_FAILED", "Native transform construction did not retain the supplied matrix."
        )
    return result


def _placement(x, y, z, rx, ry, rz):
    x, y, z = [number(v) / 1000 for v in (x, y, z)]
    a, b, c = [math.radians(number(v)) for v in (rx, ry, rz)]
    ca, sa, cb, sb, cc, sc = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)
    return [
        cc * cb,
        sc * cb,
        -sb,
        cc * sb * sa - sc * ca,
        sc * sb * sa + cc * ca,
        cb * sa,
        cc * sb * ca + sc * sa,
        sc * sb * ca - cc * sa,
        cb * ca,
        x,
        y,
        z,
        1.0,
        0.0,
        0.0,
        0.0,
    ]


def _component_record(comp):
    result = {
        "name": str(comp.Name2),
        "path": str(comp.GetPathName()),
        "configuration": str(comp.ReferencedConfiguration),
        "suppression": int(comp.GetSuppression2()),
        "fixed": bool(comp.IsFixed()),
        "visible": int(comp.Visible) != 0,
    }
    try:
        data = _transform_data(comp)
        result.update(transform=data, position_mm=[v * 1000 for v in data[9:12]])
    except CadError as exc:
        result["placement_error"] = exc.code
    return result


def list_components(session):
    rows = [_component_record(v) for v in _components(session)]
    return {"ok": True, "count": len(rows), "components": rows, "all_levels": True}


def get_mass_properties(session):
    from ..design_validation import _Reader, DesignValidationError

    model = session._require_model()
    reader = _Reader(model)
    try:
        values = reader.mass()
    except DesignValidationError as exc:
        raise CadError(exc.code, str(exc)) from exc
    native = binding.wrap(
        binding.wrap(model.Extension, "IModelDocExtension").CreateMassProperty(), "IMassProperty"
    )
    native.UseSystemUnits = True
    if not native.UseSystemUnits:
        raise CadError("MASS_UNITS_UNVERIFIED", "Native SI units were not retained.")
    values["center_of_mass_mm"] = [v * 1000 for v in vector(native.CenterOfMass, "center of mass")]
    values["surface_area_mm2"] = number(native.SurfaceArea, "surface area") * 1e6
    return {
        "ok": True,
        "mass_properties": values,
        "units": {"mass": "kg", "length": "mm", "volume": "mm3"},
        "saved": False,
        "rebuilt": False,
        "method": "native_mass_properties_SI_readback",
    }


def get_bounding_box(session):
    from ..design_validation import _Reader, DesignValidationError

    try:
        value = _Reader(session._require_model()).bounds()
    except DesignValidationError as exc:
        raise CadError(exc.code, str(exc)) from exc
    low, high, size = value["min_mm"], value["max_mm"], value["size_mm"]
    result = {
        **value,
        "min": low,
        "max": high,
        "size": size,
        **{
            f"{key}_{axis}_mm": values[i]
            for key, values in (("min", low), ("max", high), ("size", size))
            for i, axis in enumerate("xyz")
        },
    }
    return {
        "ok": True,
        "bounding_box": result,
        "bounds": value,
        "units": "mm",
        "method": "solid_body_extrema",
        "approximate": False,
        "rebuilt": False,
    }


def get_assembly_bounding_box(session):
    session._require_assembly()
    return get_bounding_box(session)


def _path(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or str(path).startswith(("\\\\", "//"))
        or path.is_symlink()
        or path.suffix.lower() not in {".sldprt", ".sldasm"}
        or not path.is_file()
    ):
        raise CadError("INVALID_COMPONENT_PATH", "Use an existing absolute local native part/assembly file.")
    return path.resolve()


def _same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError, TypeError):
        return False


def _select_component(session, value, append=False, mark=0):
    model = session._require_model()
    manager = binding.wrap(model.SelectionManager, "ISelectionMgr")
    data = binding.wrap(manager.CreateSelectData(), "ISelectData")
    data.Mark = int(mark)
    if not value.Select4(bool(append), data, False):
        raise CadError("COMPONENT_SELECTION_FAILED", "SOLIDWORKS refused the exact component selection.")


def set_component_transform(session, name, x_mm, y_mm, z_mm, rx_deg=0.0, ry_deg=0.0, rz_deg=0.0):
    value = component(session, name)
    if value.IsFixed():
        raise CadError("COMPONENT_FIXED", "A fixed component cannot receive a new placement.")
    previous = _transform_data(value)
    wanted = _placement(x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg)
    transform = _native_transform(session, wanted)
    try:
        if not value.SetTransformAndSolve2(transform):
            raise CadError(
                "TRANSFORM_SOLVE_FAILED", "Native placement/mate solving rejected the requested transform."
            )
        actual = _transform_data(value)
        if any(abs(a - b) > 1e-8 for a, b in zip(actual, wanted)):
            raise CadError(
                "TRANSFORM_READBACK_MISMATCH",
                "Constraints prevented the exact requested component placement.",
            )
        session._require_model().GraphicsRedraw2()
    except Exception as exc:
        restored = False
        try:
            value.Transform2 = _native_transform(session, previous)
            restored = all(abs(a - b) <= 1e-8 for a, b in zip(_transform_data(value), previous))
        except Exception:
            pass
        raise CadError(
            getattr(exc, "code", "TRANSFORM_FAILED"), str(exc), restoration_verified=restored
        ) from exc
    return {
        "ok": True,
        "component": _component_record(value),
        "previous_transform": previous,
        "verified": True,
        "mates_solved": True,
        "saved": False,
    }


def insert_component(session, path, x_mm=0.0, y_mm=0.0, z_mm=0.0, fixed=None):
    from .session import same_object

    path = _path(path)
    coordinates = [number(v) for v in (x_mm, y_mm, z_mm)]
    if fixed is not None and type(fixed) is not bool:
        raise CadError("INVALID_ARGUMENT", "fixed must be a boolean or null.")
    assembly, model, app = session._require_assembly(), session._require_model(), session._ensure()
    title = str(model.GetTitle())
    before = {str(v.Name2) for v in _components(session)}
    opened_source = None
    source_window = {"opened_by_operation": False, "closed_by_operation": False}
    open_documents = [binding.wrap(v, "IModelDoc2") for v in items(app.GetDocuments(), "documents")]
    found = [
        v
        for v in open_documents
        if str(v.GetPathName())
        and os.path.normcase(str(Path(v.GetPathName()).resolve())) == os.path.normcase(str(path))
    ]
    try:
        if not found:
            reply = app.OpenDoc6(str(path), 1 if path.suffix.lower() == ".sldprt" else 2, 3, "", 0, 0)
            opened = binding.wrap(reply[0] if isinstance(reply, (list, tuple)) else reply, "IModelDoc2")
            exact = opened is not None and _same_file(opened.GetPathName(), path)
            if exact and not any(same_object(opened, previous) for previous in open_documents):
                opened_source = opened
                source_window["opened_by_operation"] = True
            if not exact or isinstance(reply, (list, tuple)) and len(reply) > 1 and reply[1]:
                raise CadError("COMPONENT_LOAD_FAILED", "Cannot load the exact component file for insertion.")
            app.ActivateDoc3(title, False, 1, 0)
            if not same_object(binding.wrap(app.ActiveDoc, "IModelDoc2"), model):
                raise CadError(
                    "COMPONENT_TARGET_RESTORE_FAILED",
                    "Cannot reactivate the original assembly after loading its source.",
                )
        value = binding.wrap(
            assembly.AddComponent5(str(path), 0, "", False, "", *[v / 1000 for v in coordinates]),
            "IComponent2",
        )
        if value is None or str(value.Name2) in before:
            raise CadError(
                "COMPONENT_INSERT_FAILED", "Native insertion did not produce a new component instance."
            )
        original_fixed = bool(value.IsFixed())
        _select_component(session, value)
        assembly.UnfixComponent()
        if value.IsFixed():
            raise CadError(
                "COMPONENT_UNFIX_FAILED", "The inserted instance could not be floated for exact placement."
            )
        positioned = set_component_transform(session, str(value.Name2), *coordinates)
        requested_fixed = original_fixed if fixed is None else fixed
        if requested_fixed:
            _select_component(session, value)
            assembly.FixComponent()
        if bool(value.IsFixed()) != requested_fixed:
            raise CadError("COMPONENT_FIX_READBACK_FAILED", "Fixed/float state did not match the request.")
        positioned["component"] = _component_record(value)
        positioned["inserted"] = True
        positioned["source_window"] = source_window
        return positioned
    finally:
        if not found:
            # The shared close helper protects every other hidden document and
            # retires any source ownership before its COM handle can disconnect.
            # Never use the source handle after this close call.
            cleanup_errors = []
            if opened_source is not None:
                try:
                    source_window["cleanup"] = session.close_document(opened_source)
                    source_window["closed_by_operation"] = True
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            try:
                app.ActivateDoc3(title, False, 1, 0)
                if not same_object(binding.wrap(app.ActiveDoc, "IModelDoc2"), model):
                    raise RuntimeError("The original assembly is not active after source-window cleanup.")
            except Exception as exc:
                cleanup_errors.append(str(exc))
            if cleanup_errors:
                raise CadError(
                    "COMPONENT_SOURCE_CLEANUP_FAILED",
                    "; ".join(cleanup_errors),
                    outcome_may_be_indeterminate=True,
                    source_window=source_window,
                )


def delete_component(session, component):
    value = globals()["component"](session, component)
    model = session._require_model()
    _select_component(session, value)
    extension = binding.wrap(model.Extension, "IModelDocExtension")
    if not extension.DeleteSelection2(0):
        raise CadError("COMPONENT_DELETE_FAILED", "Native deletion was refused.")
    if any(str(v.Name2) == component for v in _components(session)):
        raise CadError(
            "COMPONENT_DELETE_UNVERIFIED", "The selected component still appears in the inventory."
        )
    return {"ok": True, "deleted": component, "verified": True, "saved": False}


def replace_component(session, name, path, reattach_mates=True):
    path = _path(path)
    if type(reattach_mates) is not bool:
        raise CadError("INVALID_ARGUMENT", "reattach_mates must be boolean.")
    target = component(session, name)
    previous = _component_record(target)
    _select_component(session, target)
    if not session._require_assembly().ReplaceComponents2(str(path), "", False, 0, reattach_mates):
        raise CadError("COMPONENT_REPLACE_FAILED", "Native component replacement was refused.")
    matches = [
        v
        for v in _components(session)
        if str(v.GetPathName())
        and _same_file(v.GetPathName(), path)
        and all(abs(a - b) <= 1e-8 for a, b in zip(_transform_data(v), previous["transform"]))
    ]
    if len(matches) != 1:
        raise CadError(
            "COMPONENT_REPLACE_UNVERIFIED",
            "Cannot identify one replacement instance at the original placement after native success.",
        )
    return {
        "ok": True,
        "previous": previous,
        "replacements": [_component_record(v) for v in matches],
        "reattach_mates_requested": reattach_mates,
        "verified": True,
        "saved": False,
    }


MATE_TYPES = {
    "coincident": 0,
    "concentric": 1,
    "perpendicular": 2,
    "parallel": 3,
    "tangent": 4,
    "distance": 5,
    "angle": 6,
    "lock": 16,
}


def add_mate(
    session,
    comp_a,
    face_a,
    comp_b,
    face_b,
    mate_type="coincident",
    distance_mm=0.0,
    angle_deg=0.0,
    flip=False,
):
    if mate_type not in MATE_TYPES or type(flip) is not bool:
        raise CadError("INVALID_ARGUMENT", "Unknown mate type or nonboolean flip.")
    distance, angle = number(distance_mm) / 1000, math.radians(number(angle_deg))
    if distance < 0 or not -2 * math.pi <= angle <= 2 * math.pi or comp_a == comp_b:
        raise CadError("INVALID_ARGUMENT", "Use distinct components and a nonnegative distance.")
    component(session, comp_a)
    component(session, comp_b)
    first = resolve(session, face_a, kind="face", component=comp_a)
    second = resolve(session, face_b, kind="face", component=comp_b)
    model, assembly = session._require_model(), session._require_assembly()
    from .session import feature_walk

    previous_features = {str(value.Name) for value in feature_walk(model)}
    model.ClearSelection2(True)
    manager = binding.wrap(model.SelectionManager, "ISelectionMgr")
    data = binding.wrap(manager.CreateSelectData(), "ISelectData")
    data.Mark = 1
    if not binding.wrap(first, "IEntity").Select4(False, data) or not binding.wrap(second, "IEntity").Select4(
        True, data
    ):
        raise CadError("MATE_SELECTION_FAILED", "Native face-pair selection failed.")
    reply = assembly.AddMate5(
        MATE_TYPES[mate_type], 2, flip, distance, 0.0, 0.0, 1.0, 1.0, angle, 0.0, 0.0, False, False, 0, 0
    )
    raw = reply[0] if isinstance(reply, (tuple, list)) else reply
    error = reply[1] if isinstance(reply, (tuple, list)) and len(reply) > 1 else 0
    # swAddMateError_e uses 1 for NoError; zero means ErrorUknown.
    if raw is None or int(error) != 1:
        raise CadError(
            "MATE_CREATION_FAILED", f"Native mate creation reported error {error}.", native_error=int(error)
        )
    mate = binding.wrap(raw, "IMate2")
    if int(mate.GetMateEntityCount()) != 2:
        raise CadError(
            "MATE_ENTITY_COUNT_UNVERIFIED", "The new mate does not expose the selected entity pair."
        )
    created = [
        value
        for value in feature_walk(model)
        if str(value.Name) not in previous_features
        and str(value.GetTypeName2()).startswith("Mate")
        and str(value.GetTypeName2()) != "MateGroup"
    ]
    if len(created) != 1:
        raise CadError("MATE_FEATURE_UNAVAILABLE", "Expected exactly one new native mate feature.")
    feature = created[0]
    code, warning = feature.GetErrorCode2()
    if code and not warning:
        raise CadError("MATE_POSTCONDITION_FAILED", f"Created mate reports native error {code}.")
    dimension_name = None
    if mate_type in {"distance", "angle"}:
        display = binding.wrap(mate.DisplayDimension2(0), "IDisplayDimension")
        dim = binding.wrap(display.GetDimension2(0), "IDimension") if display is not None else None
        wanted = distance if mate_type == "distance" else angle
        if dim is None or abs(number(dim.SystemValue) - wanted) > max(1e-10, abs(wanted) * 1e-9):
            raise CadError(
                "MATE_DIMENSION_UNVERIFIED",
                "The created dimensional mate did not retain its requested value.",
            )
        dimension_name = str(dim.GetNameForSelection())
        if not dimension_name or model.Parameter(dimension_name) is None:
            raise CadError(
                "MATE_DIMENSION_UNAVAILABLE", "The mate dimension has no exact native parameter name."
            )
    return {
        "ok": True,
        "mate": str(feature.Name),
        "type": mate_type,
        "components": [comp_a, comp_b],
        "dimension_name": dimension_name,
        "native_error": int(error),
        "verified": True,
        "saved": False,
    }


def _mate_feature(session, name):
    session._require_assembly()
    feature = session.find_feature(name)
    kind = str(feature.GetTypeName2())
    if not kind.startswith("Mate") or kind == "MateGroup":
        raise CadError(
            "MATE_REQUIRED", "The exact target must be a mate feature, not its group or another feature."
        )
    return feature


def suppress_mate(session, name, suppress=True):
    if type(suppress) is not bool:
        raise CadError("INVALID_ARGUMENT", "suppress must be boolean.")
    feature = _mate_feature(session, name)
    if not feature.SetSuppression2(0 if suppress else 1, 1, None) or bool(feature.IsSuppressed()) != suppress:
        raise CadError(
            "MATE_SUPPRESSION_FAILED", "Mate suppression state did not retain the requested value."
        )
    return {"ok": True, "mate": name, "suppressed": suppress, "verified": True, "saved": False}


def delete_mate(session, name):
    from .session import feature_walk

    feature = _mate_feature(session, name)
    if not feature.Select2(False, 0) or not binding.wrap(
        session._require_model().Extension, "IModelDocExtension"
    ).DeleteSelection2(0):
        raise CadError("MATE_DELETE_FAILED", "Native mate deletion was refused.")
    if any(str(value.Name) == name for value in feature_walk(session._require_model())):
        raise CadError("MATE_DELETE_UNVERIFIED", "The mate feature still exists after native deletion.")
    return {"ok": True, "deleted": name, "verified": True, "saved": False}


def check_interference(session):
    _components(session, require_geometry=True)
    model = session._require_model()
    manager = binding.wrap(
        session._require_assembly().InterferenceDetectionManager, "IInterferenceDetectionMgr"
    )
    if manager is None:
        raise CadError("INTERFERENCE_UNAVAILABLE", "Native interference manager is unavailable.")
    requested = {
        "TreatCoincidenceAsInterference": False,
        "TreatSubAssembliesAsComponents": False,
        "IncludeMultibodyPartInterferences": True,
    }
    previous = {key: getattr(manager, key) for key in requested}
    dirty_before = bool(model.GetSaveFlag())
    result = {"ok": False, "interferences": [], "cleanup": {"verified": False, "errors": []}, "saved": False}
    try:
        for key, value in requested.items():
            if previous[key] != value:
                setattr(manager, key, value)
            if getattr(manager, key) != value:
                raise CadError("INTERFERENCE_OPTION_REJECTED", f"Native setting {key} was not retained.")
        count = int(manager.GetInterferenceCount())
        raw = items(manager.GetInterferences(), "native interferences")
        if count != len(raw):
            raise CadError(
                "INTERFERENCE_INVENTORY_UNKNOWN", "Native count and interference objects disagree."
            )
        for item in raw:
            value = binding.wrap(item, "IInterference")
            result["interferences"].append(
                {
                    "components": [
                        str(binding.wrap(v, "IComponent2").Name2) for v in items(value.Components)
                    ],
                    "volume_mm3": number(value.Volume) * 1e9,
                }
            )
        result.update(ok=True, count=count, interference_count=count, clear=count == 0)
    finally:
        try:
            manager.Done()
        except Exception as exc:
            result["cleanup"]["errors"].append(str(exc))
        for key, value in previous.items():
            try:
                if getattr(manager, key) != value:
                    setattr(manager, key, value)
                if getattr(manager, key) != value:
                    raise RuntimeError(f"{key} restoration did not verify.")
            except Exception as exc:
                result["cleanup"]["errors"].append(str(exc))
        result["cleanup"]["verified"] = not result["cleanup"]["errors"]
        result["document_marked_dirty"] = not dirty_before and bool(model.GetSaveFlag())
        if not result["cleanup"]["verified"]:
            result.update(ok=False, status="indeterminate", code="INTERFERENCE_CLEANUP_FAILED")
    return result


def _temporary_line(session, start, end):
    modeler = binding.wrap(session._ensure().GetModeler(), "IModeler")
    direction = [b - a for a, b in zip(start, end)]
    if math.hypot(*direction) <= 1e-12:
        raise CadError("INVALID_ARGUMENT", "Temporary measurement line needs distinct endpoints.")
    curve = binding.wrap(
        modeler.CreateLine(binding.array_double(start), binding.array_double(direction)), "ICurve"
    )
    if curve is None:
        raise CadError("MEASUREMENT_GEOMETRY_FAILED", "Native modeler refused a measurement line.")
    trimmed = binding.wrap(curve.CreateTrimmedCurve2(*start, *end), "ICurve")
    body = binding.wrap(trimmed.CreateWireBody(), "IBody2") if trimmed is not None else None
    edges = items(body.GetEdges(), "temporary wire edges") if body is not None else []
    if len(edges) != 1:
        raise CadError("MEASUREMENT_GEOMETRY_FAILED", "Could not create one temporary measurement edge.")
    return body, binding.wrap(edges[0], "IEdge")


def measure_distance(session, component_a, component_b=None, point_mm=None, axis_mm=None):
    if sum(v is not None for v in (component_b, point_mm, axis_mm)) != 1:
        raise CadError("INVALID_ARGUMENT", "Choose exactly one target: component_b, point_mm, or axis_mm.")
    model = session._require_model()
    first = component(session, component_a)
    child = binding.wrap(first.GetModelDoc2(), "IModelDoc2")
    if child is None or int(child.GetType()) != 1:
        raise CadError(
            "DISTANCE_SCOPE_UNSUPPORTED", "Minimum distance currently supports resolved part instances."
        )
    temporary = None
    if component_b is not None:
        if component_b == component_a:
            raise CadError("INVALID_ARGUMENT", "Distance targets must be distinct component instances.")
        second = component(session, component_b)
        child = binding.wrap(second.GetModelDoc2(), "IModelDoc2")
        if child is None or int(child.GetType()) != 1:
            raise CadError(
                "DISTANCE_SCOPE_UNSUPPORTED",
                "Subassembly minimum distance is not accepted by the native API.",
            )
        target = {"component": component_b}
    elif point_mm is not None:
        point = [v / 1000 for v in vector(point_mm, "point_mm")]
        temporary, edge = _temporary_line(session, point, [point[0] + 0.001, point[1], point[2]])
        second = binding.wrap(edge.GetStartVertex(), "IVertex")
        if second is None or math.dist(vector(second.GetPoint(), "temporary vertex"), point) > 1e-10:
            raise CadError(
                "MEASUREMENT_POINT_UNVERIFIED", "Temporary point differs from the requested coordinates."
            )
        target = {"point_mm": list(point_mm)}
    else:
        axis = vector_pair(axis_mm, "axis_mm")
        base = [v / 1000 for v in axis[:3]]
        direction = [(axis[i + 3] - axis[i]) / 1000 for i in range(3)]
        length = math.hypot(*direction)
        if length <= 1e-12:
            raise CadError("INVALID_ARGUMENT", "Axis direction must be nonzero.")
        direction = [v / length for v in direction]
        box = vector(first.GetBox(False, False), "component bounds", 6)
        corners = [[box[i + 3 * bool(mask & (1 << i))] for i in range(3)] for mask in range(8)]
        projections = [sum((p[i] - base[i]) * direction[i] for i in range(3)) for p in corners]
        margin = max(0.001, math.dist(box[:3], box[3:]) * 0.01)
        start = [base[i] + (min(projections) - margin) * direction[i] for i in range(3)]
        end = [base[i] + (max(projections) + margin) * direction[i] for i in range(3)]
        temporary, second = _temporary_line(session, start, end)
        target = {
            "axis_mm": [axis[:3], axis[3:]],
            "axis_definition": "two_points_mm",
            "axis_semantics": "infinite_line_clipped_outside_component_bounds",
        }
    raw = model.ClosestDistance(first, second)
    if not isinstance(raw, (tuple, list)) or len(raw) != 3:
        raise CadError(
            "DISTANCE_UNAVAILABLE", "Native closest-distance call did not return both closest points."
        )
    distance, p1, p2 = raw
    distance = number(distance, "native minimum distance")
    if distance < 0:
        raise CadError("DISTANCE_UNAVAILABLE", "The native solver found no minimum-distance solution.")
    # Keep the temporary body alive until the native measurement has finished.
    del temporary
    return {
        "ok": True,
        "distance_mm": distance * 1000,
        "component_a": component_a,
        "target": target,
        "point_a_mm": [v * 1000 for v in vector(p1, "first closest point")],
        "point_b_mm": [v * 1000 for v in vector(p2, "second closest point")],
        "method": "IModelDoc2.ClosestDistance",
        "unsigned": True,
        "saved": False,
        "temporary_geometry_inserted": False,
    }


def sample_dimension(session, dimension_name, values, callback):
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 128:
        raise CadError("INVALID_ARGUMENT", "Provide 1..128 explicit dimension samples.")
    values = [number(v, "dimension sample") for v in values]
    model = session._require_model()
    dim = binding.wrap(model.Parameter(dimension_name), "IDimension")
    if dim is None or int(dim.GetType()) not in (0, 1) or int(dim.DrivenState) != 2:
        raise CadError("DRIVING_DIMENSION_REQUIRED", "Use an exact driving linear or angular dimension.")
    angular = int(dim.GetType()) == 1
    previous = number(dim.SystemValue, "initial dimension")
    dirty_before = bool(model.GetSaveFlag())
    initial_poses = {
        str(c.Name2): _transform_data(c) for c in _components(session) if int(c.GetSuppression2()) in (2, 3)
    }
    result = {
        "ok": True,
        "samples": [],
        "dimension": dimension_name,
        "unit": "deg" if angular else "mm",
        "saved": False,
        "restoration": {"verified": False, "errors": []},
    }
    try:
        for value in values:
            wanted = math.radians(value) if angular else value / 1000
            dim.SystemValue = wanted
            if not model.ForceRebuild3(False) or abs(number(dim.SystemValue) - wanted) > max(
                1e-10, abs(wanted) * 1e-9
            ):
                raise CadError(
                    "MOTION_DIMENSION_REJECTED", "A sampled dimension failed rebuild or signed readback."
                )
            result["samples"].append({"value": value, **callback(value)})
    except Exception as exc:
        result.update(ok=False, code=getattr(exc, "code", "MOTION_SAMPLE_FAILED"), error=str(exc))
    finally:
        try:
            dim.SystemValue = previous
            if not model.ForceRebuild3(False) or abs(number(dim.SystemValue) - previous) > max(
                1e-10, abs(previous) * 1e-9
            ):
                raise RuntimeError("Original driving dimension could not be restored.")
            final = {
                str(c.Name2): _transform_data(c)
                for c in _components(session)
                if int(c.GetSuppression2()) in (2, 3)
            }
            if set(final) != set(initial_poses) or any(
                any(abs(a - b) > 1e-7 for a, b in zip(final[name], pose))
                for name, pose in initial_poses.items()
            ):
                raise RuntimeError("Mate solving did not restore the initial component placements.")
            result["restoration"]["verified"] = True
        except Exception as exc:
            result["restoration"]["errors"].append(str(exc))
            result.update(ok=False, status="indeterminate", code="MOTION_RESTORE_FAILED")
        result["document_marked_dirty"] = not dirty_before and bool(model.GetSaveFlag())
    return result


def check_motion(session, dimension_name, values, distances=None):
    session._require_assembly()
    specifications = [] if distances is None else distances
    if not isinstance(specifications, list) or len(specifications) > 128:
        raise CadError(
            "INVALID_ARGUMENT", "distances must be a bounded list of explicit measurement definitions."
        )
    normalized = []
    for spec in specifications:
        if isinstance(spec, (list, tuple)) and len(spec) == 2:
            spec = {"component_a": spec[0], "component_b": spec[1]}
        if (
            not isinstance(spec, dict)
            or set(spec) - {"component_a", "component_b", "point_mm", "axis_mm", "min_mm"}
            or "component_a" not in spec
        ):
            raise CadError(
                "INVALID_ARGUMENT", "Each distance sample requires explicit component/point/axis targets."
            )
        spec = dict(spec)
        threshold = number(spec.pop("min_mm", 0.0), "minimum distance")
        if threshold < 0:
            raise CadError("INVALID_ARGUMENT", "Minimum distance cannot be negative.")
        if sum(spec.get(k) is not None for k in ("component_b", "point_mm", "axis_mm")) != 1:
            raise CadError(
                "INVALID_ARGUMENT", "A motion distance definition needs exactly one measurement target."
            )
        normalized.append((spec, threshold))

    def observe(value):
        clash = check_interference(session)
        if not clash["ok"]:
            raise CadError(
                "MOTION_INTERFERENCE_UNKNOWN", "Cannot establish interference state at a sampled position."
            )
        measured = [
            {**measure_distance(session, **spec), "minimum_mm": threshold} for spec, threshold in normalized
        ]
        return {
            "interferences": clash,
            "distances": measured,
            "passed": clash["clear"] and all(v["distance_mm"] >= v["minimum_mm"] for v in measured),
        }

    result = sample_dimension(session, dimension_name, values, observe)
    result.update(
        continuous_motion_verified=False,
        passed=result["ok"] and all(v["passed"] for v in result["samples"]),
        scope="explicit_sampled_joint_positions",
    )
    return result


def check_printability(session, up="+z", overhang_deg=45.0, min_wall_mm=None):
    from .mesh import native_triangles

    session._require_part()

    directions = {
        "+x": (1.0, 0.0, 0.0),
        "-x": (-1.0, 0.0, 0.0),
        "+y": (0.0, 1.0, 0.0),
        "-y": (0.0, -1.0, 0.0),
        "+z": (0.0, 0.0, 1.0),
        "-z": (0.0, 0.0, -1.0),
    }
    if up not in directions or not 0 < number(overhang_deg) < 90:
        raise CadError(
            "INVALID_ARGUMENT",
            "Use a signed axis and an overhang threshold strictly between 0 and 90 degrees.",
        )
    wall = number(min_wall_mm, "min_wall_mm") if min_wall_mm is not None else None
    if wall is not None and wall <= 0:
        raise CadError("INVALID_ARGUMENT", "min_wall_mm must be positive.")
    triangles = native_triangles(session)
    direction = directions[up]
    projections = [sum(a * b for a, b in zip(p, direction)) for t in triangles for p in t]
    bed = min(projections)
    risk_area = 0.0
    risks = []
    for index, (a, b, c) in enumerate(triangles):
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        norm = math.hypot(*n)
        if not norm:
            continue
        if all(abs(sum(p[i] * direction[i] for i in range(3)) - bed) <= 0.01 for p in (a, b, c)):
            continue
        down = -sum(n[i] * direction[i] for i in range(3)) / norm
        degrees = math.degrees(math.asin(max(-1.0, min(1.0, down))))
        if degrees > overhang_deg:
            risk_area += norm / 2
            if len(risks) < 100:
                risks.append(
                    {
                        "triangle": index,
                        "downward_angle_deg": degrees,
                        "centroid_mm": [(a[i] + b[i] + c[i]) / 3 for i in range(3)],
                    }
                )
    thickness = {
        "requested_minimum_mm": wall,
        "complete": False,
        "method": "opposed_planar_face_samples",
        "samples": [],
        "violations": [],
    }
    if wall is not None:
        faces = face_records(session)
        for first in faces:
            if first["surface"] != "plane" or first.get("normal") is None:
                continue
            n = first["normal"]
            p = first["center_mm"]
            distances = []
            for second in faces:
                if (
                    second["id"] == first["id"]
                    or second.get("body_index") != first.get("body_index")
                    or second.get("normal") is None
                    or sum(a * b for a, b in zip(n, second["normal"])) > -0.9999
                ):
                    continue
                candidate = resolve(session, second["id"], kind="face")
                closest = items(candidate.GetClosestPointOn(*[v / 1000 for v in p]))
                if len(closest) < 3:
                    continue
                delta = [closest[i] * 1000 - p[i] for i in range(3)]
                d = math.hypot(*delta)
                inward = sum(delta[i] * n[i] for i in range(3))
                if d > 1e-7 and inward < -1e-7 and abs(abs(inward) - d) <= 1e-5:
                    distances.append(d)
            if distances:
                value = {"face": first["id"], "thickness_mm": min(distances)}
                thickness["samples"].append(value)
                if value["thickness_mm"] < wall:
                    thickness["violations"].append(value)
    known_failure = bool(risks or thickness["violations"])
    passed = False if known_failure else None if wall is not None else True
    return {
        "ok": True,
        "passed": passed,
        "status": "fail" if known_failure else "unknown" if passed is None else "pass",
        "up": up,
        "overhang_deg": overhang_deg,
        "overhang_area_mm2": risk_area,
        "overhang_samples": risks,
        "wall_check": thickness,
        "method": "native_graphics_tessellation_screen",
        "saved": False,
        "limitations": [
            "Discrete graphics tessellation is not manufacturing or printer-path certification.",
            "Wall screening samples opposed planar faces only; untested regions remain unknown.",
        ],
    }


TOOLS = {
    name: globals()[name]
    for name in (
        "delete_mate",
        "suppress_mate",
        "delete_component",
        "get_mass_properties",
        "get_bounding_box",
        "check_printability",
        "insert_component",
        "list_components",
        "replace_component",
        "set_component_transform",
        "add_mate",
        "check_interference",
        "measure_distance",
        "check_motion",
        "get_assembly_bounding_box",
    )
}
