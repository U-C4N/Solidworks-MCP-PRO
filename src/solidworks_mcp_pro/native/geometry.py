"""Native sketch and solid construction, independently implemented for PRO.

Public coordinates are millimetres/degrees; every COM distance below is SI.
Calls are made only by the owning STA session. SOLIDWORKS API references are
the installed type library's IFeatureManager/ISketchManager declarations.
"""
from __future__ import annotations

from contextlib import contextmanager
import math

from . import binding
from .errors import CadError


EPS = 1e-9


def number(value, label, *, positive=False, nonnegative=False):
    if type(value) not in (int, float):
        raise CadError("INVALID_ARGUMENT", f"{label} must be a finite number.")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise CadError("INVALID_ARGUMENT", f"{label} cannot be represented as a native number.") from None
    if not math.isfinite(result) or abs(result) > 1e9 or (positive and result <= 0) or (nonnegative and result < 0):
        raise CadError("INVALID_ARGUMENT", f"{label} is outside its finite physical range.")
    return result


def point(value, label="point", dimensions=(2, 3)):
    if not isinstance(value, (list, tuple)) or len(value) not in dimensions:
        raise CadError("INVALID_ARGUMENT", f"{label} requires {dimensions} coordinates.")
    return [number(v, label) for v in value]


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _add(a, b):
    return [x+y for x, y in zip(a, b)]


def _scale(a, value):
    return [x*value for x in a]


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    length = _norm(a)
    if length <= EPS:
        raise CadError("DEGENERATE_GEOMETRY", "A direction or edge has zero length.")
    return _scale(a, 1/length)


def _native_point(value):
    return [coordinate/1000 for coordinate in (list(value)+[0])[:3]]


def _select(session, entity, *, mark=0, append=False):
    if entity is None:
        raise CadError("ENTITY_NOT_FOUND", "The requested native entity is unavailable.")
    if hasattr(session, "select"):
        result = session.select(entity, mark=mark, append=append)
    elif hasattr(entity, "Select2"):
        result = entity.Select2(append, mark)
    else:
        model = session._require_model()
        data = binding.wrap(model.SelectionManager, "ISelectionMgr").CreateSelectData()
        data.Mark = mark
        result = entity.Select4(append, data)
    if result is False:
        raise CadError("SELECTION_FAILED", "SOLIDWORKS refused the requested selection.")


def select_entity(entity, mark=0, append=False, model=None):
    """Select a feature/segment; optional model permits marked IEntity selection."""
    if hasattr(entity, "Select2"):
        accepted = entity.Select2(append, mark)
    else:
        data = None if model is None else binding.wrap(model.SelectionManager, "ISelectionMgr").CreateSelectData()
        if data is not None:
            data.Mark = mark
        accepted = entity.Select4(append, data)
    if accepted is False:
        raise CadError("SELECTION_FAILED", "The native entity did not select.")


def _feature(session, name):
    feature = session.find_feature(name)
    if feature is None:
        raise CadError("FEATURE_NOT_FOUND", f"No feature named {name!r} exists in the target model.")
    return binding.wrap(feature, "IFeature")


def _face(session, reference, xyz=None):
    # These APIs accept a center displaced along the support normal (add_hole
    # does not even have a Z argument). Select the requested face first; bounded
    # projection below validates its tangential position. Explicit point
    # references still retain the topology resolver's own distance check.
    face = session.resolve_face(reference)
    surface = binding.wrap(face.GetSurface(), "ISurface")
    is_plane = surface.IsPlane() if callable(getattr(surface, "IsPlane", None)) else surface.IsPlane
    if not is_plane:
        raise CadError("PLANAR_FACE_REQUIRED", "This operation requires a planar face.")
    return face


def _surface_point(face, xyz, *, allow_over_void=False):
    p = point(xyz, "face point", (3,))
    closest = face.GetClosestPointOn(*_native_point(p))
    if not isinstance(closest, (tuple, list)) or len(closest) < 3:
        raise CadError("FACE_POINT_UNAVAILABLE", "Cannot project the supplied point onto the native face.")
    projected = [number(v, "projected face point")*1000 for v in closest[:3]]
    normal = _unit(list(face.Normal))
    offset = _sub(projected, p)
    if allow_over_void:
        # A concentric counterbore starts above the clearance void. Its center
        # was already checked against the intact face before the first cut;
        # use that face's infinite planar support rather than its hole edge.
        return _add(p, _scale(normal, _dot(offset, normal)))
    tangent = _sub(offset, _scale(normal, _dot(offset, normal)))
    if _norm(tangent) > 0.01:
        raise CadError("POINT_OUTSIDE_FACE", "The supplied point projects outside the selected face boundary.")
    return projected


def _specific_features(model, types):
    from .session import feature_walk
    return [feature for feature in feature_walk(model)
            if str(feature.GetTypeName2()) in types]


def _sketch_features(model):
    return _specific_features(model, {"ProfileFeature", "3DProfileFeature"})


def _same_native_identity(first, second):
    if first is None or second is None:
        return first is second
    if not hasattr(first, "_oleobj_") or not hasattr(second, "_oleobj_"):
        return first is second
    import pythoncom
    return (first._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
            == second._oleobj_.QueryInterface(pythoncom.IID_IUnknown))


def _specific_owner(model, specific, types, *, before=None):
    """Recover the real tree IFeature without reusing a specific IDispatch."""
    candidates = _specific_features(model, types)
    matched = [feature for feature in candidates
               if _same_native_identity(feature.GetSpecificFeature2(), specific)]
    if len(matched) == 1:
        return matched[0]
    if before is not None and not matched:
        created = [feature for feature in candidates if str(feature.Name) not in before]
        if len(created) == 1:
            return created[0]
    raise CadError("FEATURE_OWNER_UNAVAILABLE", "The specific object has no unique native feature-tree owner.")


def _sketch_owner(model, sketch, *, before=None):
    return _specific_owner(model, sketch, {"ProfileFeature", "3DProfileFeature"}, before=before)


def _reference_plane(session, *constraints):
    model = session._require_part()
    before = {str(feature.Name) for feature in _specific_features(model, {"RefPlane"})}
    specific = _fm(model).InsertRefPlane(*constraints)
    if specific is None:
        raise CadError("PLANE_CREATION_FAILED", "SOLIDWORKS did not create the constrained reference plane.")
    return _specific_owner(model, specific, {"RefPlane"}, before=before)


def start_sketch(session, plane="front"):
    """Open our own 2D sketch on an exact plane/face, preserving existing edits."""
    model = session._require_part()
    if model.GetActiveSketch2() is not None:
        raise CadError("SKETCH_ALREADY_OPEN", "Finish the existing sketch before creating another.")
    before = {str(feature.Name) for feature in _sketch_features(model)}
    support = session.resolve_plane(plane) if isinstance(plane, str) else plane
    model.ClearSelection2(True)
    _select(session, support)
    manager = binding.wrap(model.SketchManager, "ISketchManager")
    manager.InsertSketch(True)
    sketch = binding.wrap(model.GetActiveSketch2(), "ISketch")
    if sketch is None:
        raise CadError("SKETCH_START_FAILED", "The requested support did not open a native sketch.")
    try:
        feature = _sketch_owner(model, sketch, before=before)
    except Exception:
        manager.InsertSketch(True)
        model.ClearSelection2(True)
        raise
    return model, manager, sketch, feature


def finish_sketch(session):
    model = session._require_part()
    sketch = binding.wrap(model.GetActiveSketch2(), "ISketch")
    if sketch is None:
        raise CadError("NO_ACTIVE_SKETCH", "No construction sketch is open.")
    feature = _sketch_owner(model, sketch)
    binding.wrap(model.SketchManager, "ISketchManager").InsertSketch(True)
    model.ClearSelection2(True)
    return feature


@contextmanager
def _sketch(session, support="front"):
    model, manager, sketch, feature = start_sketch(session, support)
    previous_add = bool(manager.AddToDB)
    try:
        manager.AddToDB = True
        yield model, manager, sketch, feature
    finally:
        manager.AddToDB = previous_add
        if model.GetActiveSketch2() is not None:
            manager.InsertSketch(True)
        model.ClearSelection2(True)


def _local(session, sketch, value, *, project=False):
    value = point(value)
    if len(value) == 2:
        return value
    utility = binding.wrap(session._ensure().GetMathUtility(), "IMathUtility")
    native = binding.wrap(utility.CreatePoint(binding.array_double(_native_point(value))), "IMathPoint")
    local = binding.wrap(native.MultiplyTransform(sketch.ModelToSketchTransform), "IMathPoint")
    data = local.ArrayData
    if not project and abs(float(data[2]))*1000 > 0.01:
        raise CadError("POINT_OFF_SKETCH_PLANE", "3D profile points must lie on the requested sketch plane.")
    return [float(data[0])*1000, float(data[1])*1000]


def _clean_points(values, *, closed=True, dimensions=(2, 3)):
    if not isinstance(values, (list, tuple)) or len(values) < (3 if closed else 2) or len(values) > 4096:
        raise CadError("INVALID_ARGUMENT", "A profile/path has an invalid number of points.")
    result = [point(v, "profile/path point", dimensions) for v in values]
    if len({len(p) for p in result}) != 1:
        raise CadError("INVALID_ARGUMENT", "All points must have the same coordinate dimension.")
    if closed and _norm(_sub(result[0], result[-1])) <= EPS:
        result.pop()
    if len(result) < (3 if closed else 2):
        raise CadError("DEGENERATE_GEOMETRY", "Not enough distinct points remain.")
    for a, b in zip(result, result[1:]+(result[:1] if closed else [])):
        if _norm(_sub(a, b)) <= EPS:
            raise CadError("DEGENERATE_GEOMETRY", "A profile/path contains a zero-length segment.")
    return result


def rotated_points(points_mm, rotate_deg=0, about_mm=None):
    points = _clean_points(points_mm)
    angle = math.radians(number(rotate_deg, "rotate_deg"))
    if not angle:
        return points
    if len(points[0]) != 2:
        raise CadError("INVALID_ARGUMENT", "Rotation is defined in sketch-local 2D coordinates.")
    origin = [0., 0.] if about_mm is None else point(about_mm, "about_mm", (2,))
    c, s = math.cos(angle), math.sin(angle)
    return [[origin[0]+c*(x-origin[0])-s*(y-origin[1]), origin[1]+s*(x-origin[0])+c*(y-origin[1])]
            for x, y in points]


def rounded_polyline(points, radii=None, *, closed=True):
    """Construct exact tangent line/arc pieces in 2D or 3D, in input units."""
    values = _clean_points(points, closed=closed)
    count = len(values)
    if radii is None:
        radii = [0.] * count
    elif type(radii) in (int, float):
        radii = [number(radii, "radius", nonnegative=True)] * count
    elif isinstance(radii, (tuple, list)) and len(radii) == count:
        radii = [number(r, "radius", nonnegative=True) for r in radii]
    else:
        raise CadError("INVALID_ARGUMENT", "Corner radii require one value or one value per vertex.")
    if not closed:
        radii = [0., *radii[1:-1], 0.]
    incoming, outgoing, trim, mids = [], [], [], []
    for i, vertex in enumerate(values):
        radius = radii[i]
        if radius == 0:
            incoming.append(vertex)
            outgoing.append(vertex)
            trim.append(0.)
            mids.append(None)
            continue
        a = _unit(_sub(values[i-1], vertex))
        b = _unit(_sub(values[(i+1) % count], vertex))
        angle = math.acos(max(-1., min(1., _dot(a, b))))
        if angle <= 1e-7 or math.pi-angle <= 1e-7:
            raise CadError("INVALID_RADIUS", "A rounded corner must have a genuine, non-reversing turn.")
        distance = radius/math.tan(angle/2)
        center = _add(vertex, _scale(_unit(_add(a, b)), radius/math.sin(angle/2)))
        incoming.append(_add(vertex, _scale(a, distance)))
        outgoing.append(_add(vertex, _scale(b, distance)))
        mids.append(_add(center, _scale(_unit(_sub(vertex, center)), radius)))
        trim.append(distance)
    segments = []
    edges = count if closed else count-1
    for i in range(edges):
        j = (i+1) % count
        length = _norm(_sub(values[j], values[i]))
        if trim[i]+trim[j] >= length-EPS:
            raise CadError("RADIUS_TOO_LARGE", "Adjacent corner radii consume an entire profile/path segment.")
        if _norm(_sub(incoming[j], outgoing[i])) > EPS:
            segments.append({"kind": "line", "start": outgoing[i], "end": incoming[j]})
        if mids[j] is not None:
            segments.append({"kind": "arc", "start": incoming[j], "end": outgoing[j], "through": mids[j]})
    return segments


def sketch_polyline(manager, points_mm, closed=True, corner_radii_mm=None):
    segments = []
    for piece in rounded_polyline(points_mm, corner_radii_mm, closed=closed):
        if piece["kind"] == "line":
            value = manager.CreateLine(*_native_point(piece["start"]), *_native_point(piece["end"]))
        else:
            value = manager.Create3PointArc(*_native_point(piece["start"]), *_native_point(piece["end"]), *_native_point(piece["through"]))
        if value is None:
            raise CadError("SKETCH_ENTITY_FAILED", "A native line or arc could not be created.")
        segments.append(binding.wrap(value, "ISketchSegment"))
    return segments


def _fm(model):
    return binding.wrap(model.FeatureManager, "IFeatureManager")


def _finish_feature(session, feature, name=None, **extra):
    if feature is None:
        raise CadError("FEATURE_CREATION_FAILED", "SOLIDWORKS returned no native feature.")
    feature = binding.wrap(feature, "IFeature")
    try:
        code, warning = feature.GetErrorCode2()
    except (AttributeError, TypeError):
        code, warning = 0, False
    if code and not warning:
        raise CadError("FEATURE_REBUILD_ERROR", f"Native feature reports error {code}.")
    return session.feature_result(feature, name=name, **extra)


def _extrude(session, sketch_feature, depth_mm, name="Extrude", *, reverse=False,
             draft_deg=0, merge=True, cut=False, symmetric=False, keep_inside=False, up_to=None):
    model = session._require_part()
    if type(reverse) is not bool or type(merge) is not bool:
        raise CadError("INVALID_ARGUMENT", "reverse and merge must be booleans.")
    draft = number(draft_deg, "draft_deg")
    if abs(draft) >= 90:
        raise CadError("INVALID_ARGUMENT", "Draft magnitude must be below 90 degrees.")
    if up_to is not None and depth_mm is not None:
        raise CadError("INVALID_ARGUMENT", "Use either a blind depth or an up-to face.")
    if not cut and depth_mm is None and up_to is None:
        raise CadError("INVALID_ARGUMENT", "A solid extrusion needs a depth or up-to face.")
    depth = 0. if depth_mm is None else number(depth_mm, "depth_mm", positive=True)/1000
    if symmetric and depth:
        depth /= 2
    model.ClearSelection2(True)
    _select(session, sketch_feature)
    end = 1 if cut and depth_mm is None else 0
    if up_to is not None:
        _select(session, session.resolve_face(up_to), append=True, mark=1)
        end = 4
    if cut:
        created = _fm(model).FeatureCut4(not symmetric, bool(keep_inside), reverse, end, end,
            depth, depth, False, False, False, False, 0., 0., False, False, False, False,
            False, True, True, False, False, False, 0, 0., False, True)
    else:
        created = _fm(model).FeatureExtrusion3(not symmetric, False, reverse, end, end, depth, depth,
            bool(draft), False, draft < 0, False, abs(math.radians(draft)), 0., False, False,
            False, False, merge, True, True, 0, 0., False)
    result = _finish_feature(session, created, name=name, sketch=str(sketch_feature.Name))
    if not cut and depth_mm is not None:
        native_name = str(binding.wrap(created, "IFeature").Name)
        parameter = model.Parameter("D1@"+native_name)
        if parameter is not None:
            parameter = binding.wrap(parameter, "IDimension")
            result["depth_dimension"] = str(parameter.GetNameForSelection())
            result.setdefault("dimensions", {})["depth"] = result["depth_dimension"]
    return result


def extrude_profile(session, points_mm, depth_mm, name="Extrude", plane="front", reverse=False,
                    draft_deg=0, merge=True, corner_radii_mm=None, rotate_deg=0, about_mm=None):
    points = rotated_points(points_mm, rotate_deg, about_mm)
    # Shape/radius viability is checked before selecting or creating native data.
    rounded_polyline(points, corner_radii_mm)
    number(depth_mm, "depth_mm", positive=True)
    draft = number(draft_deg, "draft_deg")
    if abs(draft) >= 90 or type(reverse) is not bool or type(merge) is not bool:
        raise CadError("INVALID_ARGUMENT", "Draft must be below 90 degrees; reverse and merge are booleans.")
    with _sketch(session, plane) as (_, manager, sketch, feature):
        points = [_local(session, sketch, p) for p in points]
        sketch_polyline(manager, points, corner_radii_mm=corner_radii_mm)
    return _extrude(session, feature, depth_mm, name, reverse=reverse, draft_deg=draft_deg, merge=merge)


def add_extruded_profile(session, points_mm, depth_mm, name="Extrude", corner_radii_mm=None,
                         rotate_deg=0., about_mm=None, draft_deg=0., merge=True):
    return extrude_profile(session, points_mm, depth_mm, name, corner_radii_mm=corner_radii_mm,
        rotate_deg=rotate_deg, about_mm=about_mm, draft_deg=draft_deg, merge=merge)


def add_extruded_profile_on_plane(session, points_mm, plane, depth_mm, reverse=False,
                                  name="Extrude", corner_radii_mm=None, draft_deg=0., merge=True):
    return extrude_profile(session, points_mm, depth_mm, name, plane=plane, reverse=reverse,
                           draft_deg=draft_deg, merge=merge, corner_radii_mm=corner_radii_mm)


@contextmanager
def _dimension_input(session):
    app = session._ensure()
    previous = bool(app.GetUserPreferenceToggle(10))  # swInputDimValOnCreate
    if previous:
        app.SetUserPreferenceToggle(10, False)
        if bool(app.GetUserPreferenceToggle(10)):
            raise CadError("DIMENSION_PROMPT_CONTROL_FAILED", "Cannot temporarily disable the dimension-value dialog.")
    try:
        yield
    finally:
        if previous:
            app.SetUserPreferenceToggle(10, True)
            if not bool(app.GetUserPreferenceToggle(10)):
                raise CadError("DIMENSION_PROMPT_RESTORE_FAILED", "The dimension input preference could not be restored.",
                               outcome_may_be_indeterminate=True)


def _named_dimension(session, model, entity, value_mm, position, name):
    model.ClearSelection2(True)
    # ISketchLine/ISketchArc are shape interfaces; selection is exposed by
    # ISketchSegment instead of those narrower COM interfaces.
    select_entity(binding.wrap(entity, "ISketchSegment"), model=model)
    with _dimension_input(session):
        shown = model.AddDimension2(*_native_point(position))
    if shown is None:
        raise CadError("DIMENSION_CREATION_FAILED", f"Cannot create the driving {name} dimension.")
    dimension = binding.wrap(binding.wrap(shown, "IDisplayDimension").GetDimension2(0), "IDimension")
    dimension.Name = name
    dimension.SystemValue = value_mm/1000
    if abs(float(dimension.SystemValue)-value_mm/1000) > max(1e-10, abs(value_mm)*1e-12):
        raise CadError("DIMENSION_READBACK_FAILED", f"The native {name} dimension rejected its value.")
    model.ClearSelection2(True)
    return str(dimension.GetNameForSelection())


def add_box(session, width_mm, height_mm, depth_mm, name="BlockExtrude"):
    width, height, depth = [number(v, k, positive=True) for v, k in
        ((width_mm, "width_mm"), (height_mm, "height_mm"), (depth_mm, "depth_mm"))]
    dimensions = {}
    with _sketch(session) as (model, manager, _, feature):
        manager.AddToDB = False
        native = manager.CreateCornerRectangle(0., 0., 0., width/1000, height/1000, 0.)
        if not isinstance(native, (tuple, list)) or len(native) != 4:
            raise CadError("RECTANGLE_CREATION_FAILED", "The native rectangle did not return four sides.")
        lines = [binding.wrap(segment, "ISketchLine") for segment in native]
        axes = {}
        for line in lines:
            a = binding.wrap(line.GetStartPoint2(), "ISketchPoint")
            b = binding.wrap(line.GetEndPoint2(), "ISketchPoint")
            if abs(float(a.Y)-float(b.Y)) < 1e-9:
                axes.setdefault("width", line)
            if abs(float(a.X)-float(b.X)) < 1e-9:
                axes.setdefault("height", line)
        if set(axes) != {"width", "height"}:
            raise CadError("RECTANGLE_GEOMETRY_FAILED", "The native rectangle has no orthogonal sides.")
        dimensions["width"] = _named_dimension(session, model, axes["width"], width, [width/2, -height/3], "width")
        dimensions["height"] = _named_dimension(session, model, axes["height"], height, [-width/3, height/2], "height")
        # Anchor one existing vertex; the rectangle factory supplies its corner
        # coincidences and horizontal/vertical relationships.
        model.ClearSelection2(True)
        origin = binding.wrap(lines[0].GetStartPoint2(), "ISketchPoint")
        select_entity(origin, model=model)
        model.SketchAddConstraints("sgFIXED")
    result = _extrude(session, feature, depth, name)
    result.setdefault("dimensions", {}).update(dimensions)
    return result


def _circle_sketch(session, diameter_mm, center, support="front", *, from_edges=False):
    diameter = number(diameter_mm, "diameter_mm", positive=True)
    point(center, "center")
    with _sketch(session, support) as (model, manager, sketch, feature):
        local = _local(session, sketch, center)
        circle = manager.CreateCircleByRadius(local[0]/1000, local[1]/1000, 0., diameter/2000)
        if circle is None:
            raise CadError("CIRCLE_CREATION_FAILED", "Cannot create the native circular profile.")
        dimensions = {"diameter": _named_dimension(session, model, binding.wrap(circle, "ISketchSegment"),
            diameter, [local[0]+diameter, local[1]], "diameter")}
        edge_refs = []
        if from_edges:
            dimensions, edge_refs = _edge_dimensions(session, model, manager, sketch, support, circle, local, dimensions)
    return feature, dimensions, edge_refs


def _edge_dimensions(session, model, manager, sketch, face, circle, center, dimensions):
    """Dimension circle centre to two converted nonparallel straight face edges."""
    edges = face.GetEdges() or []
    candidates = []
    for raw in edges:
        edge = binding.wrap(raw, "IEdge")
        curve = binding.wrap(edge.GetCurve(), "ICurve")
        linear = curve.IsLine() if callable(getattr(curve, "IsLine", None)) else curve.IsLine
        if not linear:
            continue
        a, b = edge.GetStartVertex(), edge.GetEndVertex()
        if a is None or b is None:
            continue
        p = _local(session, sketch, [float(v)*1000 for v in binding.wrap(a, "IVertex").GetPoint()])
        q = _local(session, sketch, [float(v)*1000 for v in binding.wrap(b, "IVertex").GetPoint()])
        direction = _unit(_sub(q, p))
        normal = [-direction[1], direction[0]]
        distance = abs(_dot(_sub(center, p), normal))
        candidates.append((distance, direction, edge))
    candidates.sort(key=lambda item: item[0])
    chosen = []
    for candidate in candidates:
        if not chosen or abs(_dot(candidate[1], chosen[0][1])) < .999:
            chosen.append(candidate)
        if len(chosen) == 2:
            break
    if len(chosen) != 2:
        raise CadError("EDGE_REFERENCES_UNAVAILABLE", "Two nonparallel straight edges are required for associative edge offsets.")
    refs = []
    center_point = binding.wrap(binding.wrap(circle, "ISketchArc").GetCenterPoint2(), "ISketchPoint")
    for index, (distance, _, edge) in enumerate(chosen):
        before = {tuple(binding.wrap(segment, "ISketchSegment").GetID())
                  for segment in (sketch.GetSketchSegments() or [])}
        model.ClearSelection2(True)
        _select(session, edge)
        if not manager.SketchUseEdge3(False, False):
            raise CadError("EDGE_PROJECTION_FAILED", "Cannot project the selected edge as an associative sketch reference.")
        current = [binding.wrap(s, "ISketchSegment") for s in (sketch.GetSketchSegments() or [])]
        added = [segment for segment in current if tuple(segment.GetID()) not in before]
        if len(added) != 1:
            raise CadError("EDGE_PROJECTION_AMBIGUOUS", "Edge conversion did not produce exactly one reference segment.")
        added[0].ConstructionGeometry = True
        model.ClearSelection2(True)
        select_entity(center_point, model=model)
        select_entity(added[0], append=True, model=model)
        with _dimension_input(session):
            shown = model.AddDimension2(center[0]/1000, center[1]/1000, 0.)
        if shown is None:
            raise CadError("EDGE_DIMENSION_FAILED", "Cannot create the circle-centre edge offset.")
        dim = binding.wrap(binding.wrap(shown, "IDisplayDimension").GetDimension2(0), "IDimension")
        dim.Name = f"edge_offset_{index+1}"
        dim.SystemValue = distance/1000
        dimensions[f"edge_offset_{index+1}"] = str(dim.GetNameForSelection())
        refs.append({"offset_mm": distance, "native_reference": "converted_face_edge"})
    return dimensions, refs


def add_disc(session, diameter_mm, thickness_mm, name="Disc", x_mm=0., y_mm=0.):
    depth = number(thickness_mm, "thickness_mm", positive=True)
    feature, dimensions, _ = _circle_sketch(session, diameter_mm, [x_mm, y_mm])
    result = _extrude(session, feature, depth, name)
    result.setdefault("dimensions", {}).update(dimensions)
    return result


def add_cylinder(session, diameter_mm, height_mm, name="Revolve"):
    radius = number(diameter_mm, "diameter_mm", positive=True)/2
    height = number(height_mm, "height_mm", positive=True)
    return add_revolved_profile(session, [[0., 0.], [radius, 0.], [radius, height], [0., height]], name=name)


def add_cone(session, bottom_diameter_mm, top_diameter_mm, height_mm, name="Revolve"):
    bottom = number(bottom_diameter_mm, "bottom_diameter_mm", positive=True)
    top = number(top_diameter_mm, "top_diameter_mm", nonnegative=True)
    height = number(height_mm, "height_mm", positive=True)
    if top >= bottom:
        raise CadError("INVALID_ARGUMENT", "The top diameter must be smaller than the bottom diameter.")
    # Lofting two exact circles would not handle the zero-radius apex. A
    # revolved trapezoid/triangle handles both cones and frusta without a mesh.
    profile = [[0., 0.], [bottom/2, 0.], [top/2, height], [0., height]]
    if top == 0:
        profile.pop(2)
    return add_revolved_profile(session, profile, name=name)


def add_extruded_spline(session, points_mm, depth_mm, name="Spline"):
    points = _clean_points(points_mm)
    number(depth_mm, "depth_mm", positive=True)
    with _sketch(session) as (_, manager, sketch, feature):
        values = [_local(session, sketch, p) for p in points]
        values.append(values[0])
        spline = manager.CreateSpline2(binding.array_double([x for p in values for x in _native_point(p)]), False)
        if spline is None:
            raise CadError("SPLINE_CREATION_FAILED", "Cannot create a closed interpolating native spline.")
    return _extrude(session, feature, depth_mm, name)


def add_revolved_profile(session, profile_mm, angle_deg=360., name="Revolve", corner_radii_mm=None, axis_mm=None):
    points = _clean_points(profile_mm, dimensions=(2,))
    angle = number(angle_deg, "angle_deg", positive=True)
    if angle > 360:
        raise CadError("INVALID_ARGUMENT", "Revolve angle cannot exceed 360 degrees.")
    axis = [[0., 0.], [0., 1.]] if axis_mm is None else _clean_points(axis_mm, closed=False, dimensions=(2,))
    if len(axis) != 2:
        raise CadError("INVALID_ARGUMENT", "axis_mm needs exactly two distinct 2D points.")
    direction = _sub(axis[1], axis[0])
    sides = [direction[0]*(p[1]-axis[0][1])-direction[1]*(p[0]-axis[0][0]) for p in points]
    if min(sides) < -EPS and max(sides) > EPS:
        raise CadError("PROFILE_CROSSES_AXIS", "A revolved profile cannot cross its rotation axis.")
    rounded_polyline(points, corner_radii_mm)
    with _sketch(session) as (_, manager, _, sketch_feature):
        sketch_polyline(manager, points, corner_radii_mm=corner_radii_mm)
        line = manager.CreateCenterLine(*_native_point(axis[0]), *_native_point(axis[1]))
        if line is None:
            raise CadError("AXIS_CREATION_FAILED", "Cannot create the revolve's construction axis.")
    model = session._require_part()
    model.ClearSelection2(True)
    _select(session, sketch_feature)
    select_entity(binding.wrap(line, "ISketchSegment"), mark=16, append=True, model=model)
    created = _fm(model).FeatureRevolve2(True, True, False, False, False, False, 0, 0,
        math.radians(angle), 0., False, False, 0., 0., 0, 0., 0., True, True, True)
    return _finish_feature(session, created, name=name, sketch=str(sketch_feature.Name))


def add_plane(session, base="front", offset_mm=0., angle_deg=0., about=None, name=None):
    offset = number(offset_mm, "offset_mm")
    angle = number(angle_deg, "angle_deg")
    if abs(angle) >= 180:
        raise CadError("INVALID_ARGUMENT", "Plane angle magnitude must be below 180 degrees.")
    if angle and not about:
        raise CadError("INVALID_ARGUMENT", "An angled plane requires an explicit edge or axis reference.")
    model = session._require_part()
    if angle and offset:
        base = add_plane(session, base=base, offset_mm=offset)["feature"]
    model.ClearSelection2(True)
    _select(session, session.resolve_plane(base))
    if angle:
        try:
            axis = _feature(session, about)
        except CadError:
            axis = session.resolve_edges([about])[0]
        _select(session, axis, mark=1, append=True)
        created = _reference_plane(session, 16 | (256 if angle < 0 else 0), abs(math.radians(angle)), 4, 0., 0, 0.)
    else:
        created = _reference_plane(session, 8 | (256 if offset < 0 else 0), abs(offset)/1000, 0, 0., 0, 0.)
    result = _finish_feature(session, created, name=name, offset_mm=offset, angle_deg=angle)
    return result


def _offset_plane(session, height_mm):
    if abs(height_mm) <= EPS:
        return "front"
    result = add_plane(session, offset_mm=height_mm)
    return result["feature"]


def _profile_sketch(session, profile, support):
    if isinstance(profile, dict):
        if set(profile) != {"center_mm", "diameter_mm"}:
            raise CadError("INVALID_ARGUMENT", "A round loft section requires center_mm and diameter_mm.")
        feature, _, _ = _circle_sketch(session, profile["diameter_mm"], point(profile["center_mm"], dimensions=(2,)), support)
        return feature
    points = _clean_points(profile)
    with _sketch(session, support) as (_, manager, sketch, feature):
        sketch_polyline(manager, [_local(session, sketch, p) for p in points])
    return feature


def loft_sketches(session, sketches, name="Loft"):
    if not isinstance(sketches, (list, tuple)) or not 2 <= len(sketches) <= 128:
        raise CadError("INVALID_ARGUMENT", "A loft requires 2..128 sketch names.")
    features = [_feature(session, item) for item in sketches]
    if len({str(f.Name) for f in features}) != len(features):
        raise CadError("INVALID_ARGUMENT", "A loft cannot repeat the same section sketch.")
    model = session._require_part()
    model.ClearSelection2(True)
    for index, feature in enumerate(features):
        _select(session, feature, mark=1, append=bool(index))
    created = _fm(model).InsertProtrusionBlend2(False, False, False, 1., 0, 0, 1., 1., False, False,
        False, 0., 0., 0, True, True, True, 0)
    return _finish_feature(session, created, name=name, section_sketches=[str(f.Name) for f in features])


def add_lofted_solid(session, profiles_mm, heights_mm, name="Loft"):
    if (not isinstance(profiles_mm, (list, tuple)) or not isinstance(heights_mm, (list, tuple))
            or len(profiles_mm) != len(heights_mm) or not 2 <= len(profiles_mm) <= 128):
        raise CadError("INVALID_ARGUMENT", "Loft sections and heights must have the same count, at least two.")
    heights = [number(h, "height_mm") for h in heights_mm]
    if any(b <= a for a, b in zip(heights, heights[1:])):
        raise CadError("INVALID_ARGUMENT", "Loft section heights must be strictly increasing.")
    # Complete pure geometry validation precedes creation of any plane/sketch.
    for profile in profiles_mm:
        if isinstance(profile, dict):
            if set(profile) != {"center_mm", "diameter_mm"}:
                raise CadError("INVALID_ARGUMENT", "Round sections require center_mm and diameter_mm.")
            point(profile["center_mm"], dimensions=(2,))
            number(profile["diameter_mm"], "diameter_mm", positive=True)
        else:
            _clean_points(profile)
    sections = [_profile_sketch(session, profile, _offset_plane(session, height))
                for profile, height in zip(profiles_mm, heights)]
    return loft_sketches(session, [str(feature.Name) for feature in sections], name)


def _path_sketch(session, path_mm, bend_radius_mm=0., smooth=False):
    points = _clean_points(path_mm, closed=False)
    points = [p+[0.] if len(p) == 2 else p for p in points]
    radius = number(bend_radius_mm, "bend_radius_mm", nonnegative=True)
    if radius and smooth:
        raise CadError("CONFLICTING_PATH_CONSTRAINTS", "Choose a smooth interpolating path or an exact circular bend radius.")
    if radius:
        rounded_polyline(points, radius, closed=False)
    model = session._require_part()
    if model.GetActiveSketch2() is not None:
        raise CadError("SKETCH_ALREADY_OPEN", "Finish the active sketch before creating a sweep path.")
    before = {str(feature.Name) for feature in _sketch_features(model)}
    manager = binding.wrap(model.SketchManager, "ISketchManager")
    model.ClearSelection2(True)
    manager.Insert3DSketch(True)
    sketch = binding.wrap(model.GetActiveSketch2(), "ISketch")
    if sketch is None:
        raise CadError("PATH_SKETCH_FAILED", "Cannot create a native 3D path sketch.")
    prior = bool(manager.AddToDB)
    segments = []
    try:
        feature = _sketch_owner(model, sketch, before=before)
        manager.AddToDB = True
        if smooth:
            native = manager.CreateSpline2(binding.array_double([x for p in points for x in _native_point(p)]), True)
            if native is None:
                raise CadError("PATH_SPLINE_FAILED", "Cannot create an interpolating 3D sweep path.")
            segments = [binding.wrap(native, "ISketchSegment")]
        else:
            segments = sketch_polyline(manager, points, closed=False, corner_radii_mm=radius)
    finally:
        manager.AddToDB = prior
        if model.GetActiveSketch2() is not None:
            manager.Insert3DSketch(True)
        model.ClearSelection2(True)
    return feature, segments, points


def _sweep(session, path, profile=None, diameter_mm=None, name="Sweep"):
    model = session._require_part()
    model.ClearSelection2(True)
    if profile is not None:
        _select(session, profile, mark=1)
    _select(session, path, mark=4, append=profile is not None)
    created = _fm(model).InsertProtrusionSwept4(False, False, 0, False, False, 0, 0,
        False, 0., 0., 0, 0, True, True, True, 0., False,
        diameter_mm is not None, 0. if diameter_mm is None else diameter_mm/1000, 0)
    return _finish_feature(session, created, name=name, path_sketch=str(path.Name))


def add_swept_pipe(session, path_mm, diameter_mm, bend_radius_mm=0., name="Pipe", smooth=False):
    diameter = number(diameter_mm, "diameter_mm", positive=True)
    if type(smooth) is not bool:
        raise CadError("INVALID_ARGUMENT", "smooth must be boolean.")
    path, _, _ = _path_sketch(session, path_mm, bend_radius_mm, smooth)
    return _sweep(session, path, diameter_mm=diameter, name=name)


def add_swept_profile(session, profile_mm, path_mm, bend_radius_mm=0., name="Sweep", corner_radii_mm=None):
    profile = _clean_points(profile_mm, dimensions=(2,))
    rounded_polyline(profile, corner_radii_mm)
    path, segments, _ = _path_sketch(session, path_mm, bend_radius_mm)
    model = session._require_part()
    first = binding.wrap(segments[0], "ISketchLine")
    endpoint = binding.wrap(first.GetStartPoint2(), "ISketchPoint")
    model.ClearSelection2(True)
    _select(session, segments[0])
    _select(session, endpoint, mark=1, append=True)
    plane = _reference_plane(session, 2, 0., 4, 0., 0, 0.)
    with _sketch(session, plane) as (_, manager, _, profile_feature):
        sketch_polyline(manager, profile, corner_radii_mm=corner_radii_mm)
    return _sweep(session, path, profile_feature, name=name)


def _face_circle(session, diameter_mm, face, xyz, *, depth_mm=None, height_mm=None, name="Hole", from_edges=False,
                 _over_void=False):
    if type(from_edges) is not bool:
        raise CadError("INVALID_ARGUMENT", "from_edges must be boolean.")
    if depth_mm is not None:
        number(depth_mm, "depth_mm", positive=True)
    if height_mm is not None:
        number(height_mm, "height_mm", positive=True)
    support = _face(session, face, xyz)
    projected = _surface_point(support, xyz, allow_over_void=_over_void)
    feature, dimensions, references = _circle_sketch(session, diameter_mm, projected, support, from_edges=from_edges)
    result = _extrude(session, feature, height_mm if height_mm is not None else depth_mm,
                      name, cut=height_mm is None)
    result.setdefault("dimensions", {}).update(dimensions)
    if references:
        result["measured_from"] = references
    result["center_mm"] = projected
    return result


def add_hole(session, diameter_mm, x_mm, y_mm, name="Hole"):
    return _face_circle(session, diameter_mm, "+z", [x_mm, y_mm, 0.], name=name)


def add_counterbore_hole(session, clearance_diameter_mm, cbore_diameter_mm, cbore_depth_mm,
                         x_mm, y_mm, name="Counterbore"):
    clearance = number(clearance_diameter_mm, "clearance_diameter_mm", positive=True)
    bore = number(cbore_diameter_mm, "cbore_diameter_mm", positive=True)
    depth = number(cbore_depth_mm, "cbore_depth_mm", positive=True)
    if bore <= clearance:
        raise CadError("INVALID_ARGUMENT", "Counterbore diameter must exceed clearance diameter.")
    add_hole(session, clearance, x_mm, y_mm, name+"_Clearance")
    return _face_circle(session, bore, "+z", [x_mm, y_mm, 0.], depth_mm=depth, name=name, _over_void=True)


def add_hole_on_face(session, diameter_mm, face, x_mm, y_mm, z_mm, depth_mm=None,
                      name="Hole", from_edges=False):
    return _face_circle(session, diameter_mm, face, [x_mm, y_mm, z_mm], depth_mm=depth_mm,
                        name=name, from_edges=from_edges)


def add_boss_on_face(session, diameter_mm, face, x_mm, y_mm, z_mm, height_mm,
                      name="Boss", from_edges=False):
    return _face_circle(session, diameter_mm, face, [x_mm, y_mm, z_mm], height_mm=height_mm,
                        name=name, from_edges=from_edges)


def add_extruded_profile_on_face(session, points_mm, face, depth_mm, name="Boss", corner_radii_mm=None):
    support = _face(session, face)
    return extrude_profile(session, points_mm, depth_mm, name, plane=support, corner_radii_mm=corner_radii_mm)


def _cut_polygon(session, points_mm, support, depth_mm=None, name="Cut", corner_radii_mm=None,
                 symmetric=False, keep_inside=False, rotate_deg=0., about_mm=None, model_xy=False):
    points = rotated_points(points_mm, rotate_deg, about_mm)
    rounded_polyline(points, corner_radii_mm)
    if depth_mm is not None:
        number(depth_mm, "depth_mm", positive=True)
    with _sketch(session, support) as (_, manager, sketch, feature):
        local = [_local(session, sketch, p+[0.], project=True) if model_xy and len(p) == 2
                 else _local(session, sketch, p) for p in points]
        sketch_polyline(manager, local, corner_radii_mm=corner_radii_mm)
    return _extrude(session, feature, depth_mm, name, cut=True, symmetric=symmetric, keep_inside=keep_inside)


def cut_profile(session, points_mm, depth_mm=None, name="Cut", corner_radii_mm=None, rotate_deg=0., about_mm=None):
    return _cut_polygon(session, points_mm, _face(session, "+z"), depth_mm, name,
                        corner_radii_mm, rotate_deg=rotate_deg, about_mm=about_mm, model_xy=True)


def cut_profile_on_face(session, points_mm, face, depth_mm=None, name="Cut", corner_radii_mm=None):
    return _cut_polygon(session, points_mm, _face(session, face), depth_mm, name, corner_radii_mm)


def cut_profile_through_plane(session, points_mm, plane, depth_mm=None, name="Cut",
                               corner_radii_mm=None, keep_inside=False):
    if type(keep_inside) is not bool or (keep_inside and depth_mm is not None):
        raise CadError("INVALID_ARGUMENT", "keep_inside is boolean and is supported only with a through-all cut.")
    return _cut_polygon(session, points_mm, plane, depth_mm, name, corner_radii_mm,
                        symmetric=True, keep_inside=keep_inside)


def cut_offset_pocket(session, face, x_mm, y_mm, z_mm, rim_mm, depth_mm=None, name="Pocket"):
    rim = number(rim_mm, "rim_mm", positive=True)
    if depth_mm is not None:
        number(depth_mm, "depth_mm", positive=True)
    support = _face(session, face, [x_mm, y_mm, z_mm])
    _surface_point(support, [x_mm, y_mm, z_mm])
    with _sketch(session, support) as (model, manager, sketch, feature):
        _select(session, support)
        if not manager.SketchUseEdge3(True, True):
            raise CadError("OUTLINE_CONVERSION_FAILED", "Cannot convert the native face outline and inner loops.")
        model.ClearSelection2(True)
        segments = [binding.wrap(s, "ISketchSegment") for s in (sketch.GetSketchSegments() or [])]
        if not segments:
            raise CadError("OUTLINE_EMPTY", "The selected face has no usable planar boundary.")
        for i, segment in enumerate(segments):
            select_entity(segment, append=bool(i), model=model)
        # A negative offset is toward the interior for the outer loop. The
        # native chain solver retains linked circular/spline boundary shapes.
        if not manager.SketchOffset2(-rim/1000, False, True, 0, 1, True):
            raise CadError("POCKET_OFFSET_FAILED", "The requested rim cannot be offset inside this native face.")
        for segment in segments:
            segment.ConstructionGeometry = True
    return _extrude(session, feature, depth_mm, name, cut=True)


def slot_outline(start_mm, end_mm, width_mm):
    start = point(start_mm, "start_mm", (2,))
    end = point(end_mm, "end_mm", (2,))
    width = number(width_mm, "width_mm", positive=True)
    tangent = _unit(_sub(end, start))
    normal = [-tangent[1], tangent[0]]
    radius = width/2
    a, b = _add(start, _scale(normal, radius)), _add(end, _scale(normal, radius))
    c, d = _sub(end, _scale(normal, radius)), _sub(start, _scale(normal, radius))
    return [{"kind": "line", "start": a, "end": b},
            {"kind": "arc", "start": b, "end": c, "through": _add(end, _scale(tangent, radius))},
            {"kind": "line", "start": c, "end": d},
            {"kind": "arc", "start": d, "end": a, "through": _sub(start, _scale(tangent, radius))}]


def _draw_pieces(manager, pieces):
    result = []
    for piece in pieces:
        if piece["kind"] == "line":
            segment = manager.CreateLine(*_native_point(piece["start"]), *_native_point(piece["end"]))
        else:
            segment = manager.Create3PointArc(*_native_point(piece["start"]), *_native_point(piece["end"]), *_native_point(piece["through"]))
        if segment is None:
            raise CadError("SKETCH_ENTITY_FAILED", "The native slot line/arc was not created.")
        result.append(segment)
    return result


def add_extruded_slot(session, start_mm, end_mm, width_mm, depth_mm, name="Slot"):
    pieces = slot_outline(start_mm, end_mm, width_mm)
    number(depth_mm, "depth_mm", positive=True)
    with _sketch(session) as (_, manager, _, feature):
        _draw_pieces(manager, pieces)
    return _extrude(session, feature, depth_mm, name)


def cut_slot(session, length_mm, width_mm, x_mm, y_mm, angle_deg=0., depth_mm=None, name="Slot"):
    length = number(length_mm, "length_mm", positive=True)
    width = number(width_mm, "width_mm", positive=True)
    center = point([x_mm, y_mm], dimensions=(2,))
    angle = math.radians(number(angle_deg, "angle_deg"))
    if length <= width:
        raise CadError("INVALID_ARGUMENT", "Slot overall length must exceed its width.")
    if depth_mm is not None:
        number(depth_mm, "depth_mm", positive=True)
    half = (length-width)/2
    offset = [half*math.cos(angle), half*math.sin(angle)]
    pieces = slot_outline(_sub(center, offset), _add(center, offset), width)
    support = _face(session, "+z")
    with _sketch(session, support) as (_, manager, sketch, feature):
        # Public coordinates for this primitive remain XY model coordinates.
        projected = []
        for piece in pieces:
            projected.append({key: _local(session, sketch, value+[0.], project=True) if key in {"start", "end", "through"} else value
                              for key, value in piece.items()})
        _draw_pieces(manager, projected)
    return _extrude(session, feature, depth_mm, name, cut=True)


def extrude_sketch(session, sketch, depth_mm=None, reverse=False, name="Extrude", up_to=None):
    return _extrude(session, _feature(session, sketch), depth_mm, name, reverse=reverse, up_to=up_to)


def cut_sketch(session, sketch, depth_mm=None, reverse=False, name="Cut"):
    return _extrude(session, _feature(session, sketch), depth_mm, name, reverse=reverse, cut=True)


def _tangent_midpoint(start, end, tangent):
    tangent = _unit(tangent)
    normal = [-tangent[1], tangent[0]]
    chord = _sub(end, start)
    denominator = 2*_dot(chord, normal)
    if abs(denominator) <= EPS:
        raise CadError("INVALID_TANGENT_ARC", "A tangent arc cannot end on its starting tangent line.")
    radius_signed = _dot(chord, chord)/denominator
    center = _add(start, _scale(normal, radius_signed))
    a = math.atan2(start[1]-center[1], start[0]-center[0])
    b = math.atan2(end[1]-center[1], end[0]-center[0])
    sweep = (b-a) % (2*math.pi) if radius_signed > 0 else -((a-b) % (2*math.pi))
    middle = a+sweep/2
    return [center[0]+abs(radius_signed)*math.cos(middle), center[1]+abs(radius_signed)*math.sin(middle)]


def _arc_tangent(start, through, end):
    ax, ay = start
    bx, by = through
    cx, cy = end
    denominator = 2*(ax*(by-cy)+bx*(cy-ay)+cx*(ay-by))
    if abs(denominator) <= EPS:
        raise CadError("INVALID_ARC", "Arc endpoints and through-point are collinear.")
    aa, bb, cc = ax*ax+ay*ay, bx*bx+by*by, cx*cx+cy*cy
    center = [(aa*(by-cy)+bb*(cy-ay)+cc*(ay-by))/denominator,
              (aa*(cx-bx)+bb*(ax-cx)+cc*(bx-ax))/denominator]
    sign = 1 if denominator > 0 else -1
    radius = _sub(end, center)
    return _scale(_unit([-radius[1], radius[0]]), sign)


def add_sketch(session, plane, start_mm, segments, name=None):
    start = point(start_mm, "start_mm", (2,))
    if not isinstance(segments, (tuple, list)) or not 1 <= len(segments) <= 4096:
        raise CadError("INVALID_ARGUMENT", "segments must be a nonempty bounded list.")
    current, tangent, pieces = start, None, []
    for item in segments:
        if not isinstance(item, dict) or set(item)-{"line", "arc", "spline", "through", "tangent"}:
            raise CadError("INVALID_ARGUMENT", "Sketch segments accept line, arc, spline, through and tangent fields.")
        kinds = {key for key in ("line", "arc", "spline") if key in item}
        if len(kinds) != 1:
            raise CadError("INVALID_ARGUMENT", "Each segment must name exactly one geometry type.")
        kind = next(iter(kinds))
        if "tangent" in item and type(item["tangent"]) is not bool:
            raise CadError("INVALID_ARGUMENT", "tangent must be boolean when supplied.")
        if "through" in item and kind != "arc":
            raise CadError("INVALID_ARGUMENT", "Only an arc accepts a through-point.")
        if kind == "spline":
            if item.get("tangent"):
                raise CadError("SPLINE_TANGENCY_UNSUPPORTED", "Interpolated spline segments do not support an explicit tangent constraint.")
            pts = [current]+[point(p, "spline point", (2,)) for p in item[kind]]
            _clean_points(pts, closed=False)
            pieces.append({"kind": "spline", "points": pts})
            tangent = _unit(_sub(pts[-1], pts[-2]))
            current = pts[-1]
        else:
            end = point(item[kind], "segment endpoint", (2,))
            if _norm(_sub(end, current)) <= EPS:
                raise CadError("DEGENERATE_GEOMETRY", "Sketch segments cannot have zero endpoint distance.")
            if kind == "line":
                next_tangent = _unit(_sub(end, current))
                if item.get("tangent") and (tangent is None or _dot(tangent, next_tangent) < 1-1e-6):
                    raise CadError("TANGENCY_MISMATCH", "The requested line is not tangent to its predecessor.")
                pieces.append({"kind": "line", "start": current, "end": end})
                tangent = next_tangent
            else:
                if "through" in item:
                    through = point(item["through"], "arc through-point", (2,))
                    if item.get("tangent"):
                        starting_tangent = _scale(_arc_tangent(end, through, current), -1)
                        if tangent is None or _dot(tangent, starting_tangent) < 1-1e-6:
                            raise CadError("TANGENCY_MISMATCH", "The requested arc is not tangent to its predecessor.")
                elif item.get("tangent") and tangent is not None:
                    through = _tangent_midpoint(current, end, tangent)
                else:
                    raise CadError("INVALID_ARGUMENT", "An arc requires a through-point or a preceding tangent direction.")
                tangent = _arc_tangent(current, through, end)
                pieces.append({"kind": "arc", "start": current, "end": end, "through": through})
            current = end
    with _sketch(session, plane) as (_, manager, _, feature):
        for piece in pieces:
            if piece["kind"] == "spline":
                created = manager.CreateSpline2(binding.array_double([x for p in piece["points"] for x in _native_point(p)]), False)
                if created is None:
                    raise CadError("SPLINE_CREATION_FAILED", "Cannot create the native sketch spline.")
            else:
                _draw_pieces(manager, [piece])
    result = _finish_feature(session, feature, name=name, closed=_norm(_sub(start, current)) <= EPS,
                             segment_count=len(pieces))
    result["sketch"] = result["feature"]
    return result


def add_rib(session, start_mm, end_mm, toward_mm, thickness_mm, z_mm, name="Rib"):
    a, b, toward = [point(v, k, (2,)) for v, k in ((start_mm, "start_mm"), (end_mm, "end_mm"), (toward_mm, "toward_mm"))]
    thickness = number(thickness_mm, "thickness_mm", positive=True)
    height = number(z_mm, "z_mm")
    direction, fill = _sub(b, a), _sub(toward, a)
    cross = direction[0]*fill[1]-direction[1]*fill[0]
    if abs(cross) <= EPS or _norm(direction) <= EPS:
        raise CadError("INVALID_RIB_DIRECTION", "A rib needs a nonzero free edge and a point off that edge.")
    model = session._require_part()
    bodies = list(binding.wrap(model, "IPartDoc").GetBodies2(0, False) or [])
    if len(bodies) != 1:
        raise CadError("RIB_BODY_AMBIGUOUS", "This rib requires one unambiguous existing solid body.")
    body = binding.wrap(bodies[0], "IBody2")
    with _sketch(session, _offset_plane(session, height)) as (_, manager, _, feature):
        sketch_polyline(manager, [a, b], closed=False)
    model.ClearSelection2(True)
    _select(session, feature)
    selection = binding.wrap(model.SelectionManager, "ISelectionMgr")
    data = binding.wrap(selection.CreateSelectData(), "ISelectData")
    data.Mark = 1
    # Rib's native multibody contract requires the input body at mark 1.
    # IBody2.Select2 takes ISelectData, unlike IFeature.Select2's integer mark.
    if not body.Select2(True, data):
        raise CadError("RIB_BODY_SELECTION_FAILED", "The native rib input body did not select.")
    before = {str(item.Name) for item in _specific_features(model, {"Rib"})}
    # InsertRib is a native void method, so success must be established from
    # exactly one new tree feature rather than from a nonexistent return value.
    # The native reference edge's default material side is its right side;
    # a positive 2D cross product requests the reversed (left) material side.
    _fm(model).InsertRib(True, False, thickness/1000, 0, cross > 0,
                         False, False, 0., False, False)
    created = [item for item in _specific_features(model, {"Rib"}) if str(item.Name) not in before]
    if len(created) != 1:
        raise CadError("RIB_CREATION_FAILED", "The native rib operation did not create exactly one new Rib feature.",
                       reverse_material_direction=cross > 0, free_edge_mm=[a, b], toward_mm=toward,
                       support_z_mm=height, new_rib_count=len(created))
    return _finish_feature(session, created[0], name=name, thickness_mm=thickness, growth_toward_mm=toward)


def add_text_on_face(session, text, face, x_mm, y_mm, z_mm, height_mm, depth_mm,
                     emboss=False, font=None, name="Text"):
    if not isinstance(text, str) or not text.strip() or len(text) > 4096 or "\x00" in text:
        raise CadError("INVALID_ARGUMENT", "text must contain 1..4096 visible characters.")
    height = number(height_mm, "height_mm", positive=True)
    depth = number(depth_mm, "depth_mm", positive=True)
    if type(emboss) is not bool or (font is not None and (not isinstance(font, str) or not font.strip())):
        raise CadError("INVALID_ARGUMENT", "emboss must be boolean and font must be a nonempty name.")
    support = _face(session, face, [x_mm, y_mm, z_mm])
    projected = _surface_point(support, [x_mm, y_mm, z_mm])
    with _sketch(session, support) as (model, _, sketch, feature):
        position = _local(session, sketch, projected)
        native = binding.wrap(model.InsertSketchText(position[0]/1000, position[1]/1000, 0., text,
            0, 0, 0, 100, 100), "ISketchText")
        if native is None:
            raise CadError("TEXT_CREATION_FAILED", "The installed font could not create native sketch text.")
        formatting = binding.wrap(native.GetTextFormat(), "ITextFormat")
        formatting.CharHeight = height/1000
        if font is not None:
            formatting.TypeFaceName = font
        if native.SetTextFormat(False, formatting) is False:
            raise CadError("TEXT_FORMAT_FAILED", "Native text formatting was rejected.")
        actual = binding.wrap(native.GetTextFormat(), "ITextFormat")
        if abs(float(actual.CharHeight)-height/1000) > max(1e-10, height*1e-10):
            raise CadError("TEXT_FORMAT_READBACK_FAILED", "Native text did not retain the requested character height.")
    return _extrude(session, feature, depth, name, cut=not emboss)


def add_hole_wizard(session, kind, size, face, x_mm, y_mm, z_mm, depth_mm=None,
                     fit="normal", thread="cosmetic", name=None):
    choices = {"clearance": (2, 144), "counterbore": (0, 139), "countersink": (1, 140), "tapped": (4, 147)}
    if kind not in choices or fit not in {"close", "normal", "loose"} or thread not in {"cosmetic", "modeled"}:
        raise CadError("INVALID_ARGUMENT", "Use an ISO clearance/counterbore/countersink/tapped hole and documented fit/thread mode.")
    if not isinstance(size, str) or len(size) > 64 or not size.strip():
        raise CadError("INVALID_ARGUMENT", "size must name an installed ISO hole-table entry.")
    if depth_mm is not None:
        number(depth_mm, "depth_mm", positive=True)
    support = _face(session, face, [x_mm, y_mm, z_mm])
    projected = _surface_point(support, [x_mm, y_mm, z_mm])
    model = session._require_part()
    model.ClearSelection2(True)
    # Native selection-point coordinates determine the wizard's initial hole
    # position; the created positioning sketch is then explicitly read/set.
    extension = binding.wrap(model.Extension, "IModelDocExtension")
    # Generated InvokeTypes already declares Callout as VT_DISPATCH. A VARIANT
    # wrapper cannot be coerced to that native interface; None is its null.
    if not extension.SelectByID2("", "FACE", *_native_point(projected), False, 0, None, 0):
        raise CadError("FACE_SELECTION_FAILED", "Cannot select the hole wizard's exact face point.")
    generic, fastener = choices[kind]
    created = _fm(model).HoleWizard5(generic, 8, fastener, size, 1 if depth_mm is None else 0,
        0., 0. if depth_mm is None else depth_mm/1000, 0., 0., 0., 0., 0., 0., 0., 0., 0.,
        0., 0., 0., 0., "", False, True, True, False, False, False)
    if created is None:
        raise CadError("HOLE_STANDARD_UNAVAILABLE", "The native Hole Wizard rejected that installed ISO size/type.")
    feature = binding.wrap(created, "IFeature")
    data = binding.wrap(feature.GetDefinition(), "IWizardHoleFeatureData2")
    if data is None or not data.AccessSelections(model, None):
        raise CadError("HOLE_DEFINITION_UNAVAILABLE", "Cannot verify the native Hole Wizard definition.")
    try:
        data.HoleFit = {"close": 0, "normal": 1, "loose": 2}[fit]
        if kind == "tapped":
            data.CosmeticThreadType = 1 if thread == "cosmetic" else 0
        if not feature.ModifyDefinition(data, model, None):
            raise CadError("HOLE_DEFINITION_REJECTED", "Native hole-fit/thread settings did not apply.")
    finally:
        data.ReleaseSelectionAccess()
    data = binding.wrap(feature.GetDefinition(), "IWizardHoleFeatureData2")
    positions = list(data.GetSketchPoints() or [])
    if len(positions) != 1:
        raise CadError("HOLE_POSITION_AMBIGUOUS", "A new wizard hole must have exactly one positioning point.")
    position = binding.wrap(positions[0], "ISketchPoint")
    positioning_sketch = binding.wrap(position.GetSketch(), "ISketch")
    is_3d = bool(positioning_sketch.Is3D())
    local = projected if is_3d else _local(session, positioning_sketch, projected)+[0.]
    expected = [value/1000 for value in local]
    model.ClearSelection2(True)
    _select(session, _sketch_owner(model, positioning_sketch))
    model.EditSketch()
    editing = binding.wrap(model.GetActiveSketch2(), "ISketch")
    if not _same_native_identity(editing, positioning_sketch):
        raise CadError("HOLE_POSITION_EDIT_FAILED", "The exact Hole Wizard positioning sketch did not enter edit mode.")
    try:
        if not position.SetCoords(*expected):
            raise CadError("HOLE_POSITION_REJECTED", "The native position point rejected its coordinates.",
                           requested_sketch_mm=local)
    finally:
        manager = binding.wrap(model.SketchManager, "ISketchManager")
        if is_3d:
            manager.Insert3DSketch(True)
        else:
            manager.InsertSketch(True)
        model.ClearSelection2(True)
    if model.ForceRebuild3(False) is False:
        raise CadError("HOLE_REBUILD_FAILED", "The positioned standard hole failed to rebuild.")
    data = binding.wrap(feature.GetDefinition(), "IWizardHoleFeatureData2")
    rebuilt_positions = list(data.GetSketchPoints() or [])
    if len(rebuilt_positions) != 1:
        raise CadError("HOLE_POSITION_AMBIGUOUS", "The rebuilt hole does not have exactly one position.")
    actual_point = binding.wrap(rebuilt_positions[0], "ISketchPoint")
    actual = [float(actual_point.X), float(actual_point.Y), float(actual_point.Z)]
    if any(abs(value-wanted) > 1e-8 for value, wanted in zip(actual, expected)):
        raise CadError("HOLE_POSITION_READBACK_FAILED", "The rebuilt hole positioning sketch rejected its requested location.",
                       requested_sketch_mm=local, actual_sketch_mm=[value*1000 for value in actual], sketch_3d=is_3d)
    if str(data.FastenerSize).strip().casefold() != size.strip().casefold():
        raise CadError("HOLE_SIZE_READBACK_FAILED", "The native wizard silently selected a different standard size.")
    if int(data.HoleFit) != {"close": 0, "normal": 1, "loose": 2}[fit]:
        raise CadError("HOLE_FIT_READBACK_FAILED", "The native wizard did not retain the requested hole fit.")
    hole = {"standard": "ISO", "kind": kind, "size": str(data.FastenerSize), "fit": fit,
            "diameter_mm": float(data.Diameter)*1000, "depth_mm": float(data.Depth)*1000}
    result = _finish_feature(session, feature, name=name or "HoleWizard", hole=hole, center_mm=projected)
    if kind == "tapped" and thread == "modeled":
        from .features import add_thread
        thread_depth = depth_mm
        if thread_depth is None:
            thread_depth = max(float(data.ThreadDepth)*1000, float(data.Depth)*1000)
        if thread_depth <= 0:
            raise CadError("THREAD_DEPTH_UNAVAILABLE", "A modeled through-tap requires a measurable native thread depth.")
        result["modeled_thread"] = add_thread(session, size, *projected, thread_depth, internal=True,
            name=(name or "HoleWizard")+"_Thread")
    return result


TOOLS = {function.__name__: function for function in (
    add_box, add_extruded_profile, add_extruded_spline, add_disc, add_cylinder, add_cone,
    add_revolved_profile, add_swept_pipe, add_swept_profile, add_lofted_solid, loft_sketches,
    cut_profile_through_plane, add_plane, add_extruded_profile_on_plane, add_rib, add_hole,
    add_counterbore_hole, add_hole_on_face, add_hole_wizard, add_boss_on_face,
    add_extruded_profile_on_face, add_text_on_face, cut_profile, cut_profile_on_face,
    cut_offset_pocket, cut_slot, add_extruded_slot, extrude_sketch, add_sketch, cut_sketch,
)}
