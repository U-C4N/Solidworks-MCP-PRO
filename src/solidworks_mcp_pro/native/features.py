"""Independent native feature operations built against the SOLIDWORKS API.

Public quantities are millimetres, degrees and kilograms. Native objects stay
on the session's STA, and the session owns selection/transaction restoration.
No implementation from another CAD automation package is used here.
"""
from __future__ import annotations

from contextlib import contextmanager
from itertools import combinations
import hashlib
import math
from pathlib import Path
import re
from uuid import uuid4
import xml.etree.ElementTree as ET

from . import binding
from .errors import CadError


def _number(value, label, *, positive=False):
    if type(value) not in (float, int):
        raise CadError("INVALID_ARGUMENT", f"{label} must be a finite number.")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise CadError("INVALID_ARGUMENT", f"{label} is outside native numeric range.") from None
    if not math.isfinite(result) or positive and result <= 0:
        raise CadError("INVALID_ARGUMENT", f"{label} must be {'positive and ' if positive else ''}finite.")
    return result


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise CadError("INVALID_ARGUMENT", f"{label} must be an exact nonempty name.")
    return value


def _array(value, label):
    if value is None:
        return []
    if not isinstance(value, (tuple, list)) or len(value) > 20000:
        raise CadError("NATIVE_DATA_INVALID", f"Unexpected native {label} array.")
    return list(value)


def _same(left, right):
    return left is right or getattr(left, "_oleobj_", left) == getattr(right, "_oleobj_", right)


def _distinct(values):
    result = []
    for value in values:
        if value is not None and not any(_same(value, prior) for prior in result):
            result.append(value)
    return result


def _objects(values):
    return binding.array_dispatch(values)


def _doubles(values):
    return binding.array_double(values)


def _model(session, *, part=False):
    return session._require_part() if part else session._require_model()


def _manager(model):
    return binding.wrap(model.FeatureManager, "IFeatureManager")


def _extension(model):
    return binding.wrap(model.Extension, "IModelDocExtension")


def _walk(model):
    """Iterate native history and absorbed subfeatures once, with a bound."""
    roots, seen = [], set()
    current = binding.wrap(model.FirstFeature(), "IFeature")
    while current is not None:
        key = str(current.Name), str(current.GetTypeName2())
        if key in seen or len(seen) >= 20000:
            raise CadError("FEATURE_HISTORY_INVALID", "Feature traversal is cyclic or unbounded.")
        seen.add(key)
        roots.append(current)
        current = binding.wrap(current.GetNextFeature(), "IFeature")
    todo = [(f, None) for f in reversed(roots)]
    visited = set()
    while todo:
        feature, parent = todo.pop()
        key = str(feature.Name), str(feature.GetTypeName2())
        if key in visited:
            continue
        visited.add(key)
        if len(visited) > 20000:
            raise CadError("FEATURE_HISTORY_INVALID", "Feature traversal exceeds its bound.")
        yield feature, parent
        children, child_seen = [], set()
        child = binding.wrap(feature.GetFirstSubFeature(), "IFeature")
        while child is not None:
            ckey = str(child.Name), str(child.GetTypeName2())
            if ckey in child_seen or len(child_seen) > 20000:
                raise CadError("FEATURE_HISTORY_INVALID", "Subfeature traversal is cyclic.")
            child_seen.add(ckey)
            children.append(child)
            child = binding.wrap(child.GetNextSubFeature(), "IFeature")
        todo.extend((c, str(feature.Name)) for c in reversed(children))


def _feature(session, name):
    name = _text(name, "Feature name")
    matches = [f for f, _ in _walk(_model(session)) if str(f.Name) == name]
    if len(matches) != 1:
        raise CadError("FEATURE_NOT_UNIQUE", f"Expected one feature named {name!r}; found {len(matches)}.")
    return matches[0]


def _last_solid_feature(session):
    entries = [f for f, parent in _walk(_model(session, part=True)) if parent is None
               and str(f.GetTypeName2()) not in {"RefPlane", "OriginProfileFeature", "HistoryFolder", "SolidBodyFolder",
                                                "SurfaceBodyFolder", "SensorFolder", "CommentsFolder", "MaterialFolder",
                                                "SelectionSetFolder", "ProfileFeature", "3DProfileFeature", "RefAxis"}]
    if not entries:
        raise CadError("NO_SEED_FEATURE", "Supply an exact seed feature name.")
    return entries[-1]


def _bodies(session):
    part = binding.wrap(_model(session, part=True), "IPartDoc")
    return [binding.wrap(b, "IBody2") for b in _array(part.GetBodies2(0, False), "solid bodies")]


def _body(session, reference):
    bodies = _bodies(session)
    stamp = int(_model(session).GetUpdateStamp())
    if isinstance(reference, str) and re.fullmatch(r"B\d+_\d+", reference):
        epoch, index = map(int, reference[1:].split("_"))
        if epoch != stamp:
            raise CadError("STALE_BODY_REFERENCE", "Body reference predates a model change; list bodies again.")
        if 0 < index <= len(bodies):
            return bodies[index - 1]
    matches = [body for body in bodies if str(body.Name) == reference]
    if len(matches) != 1:
        raise CadError("BODY_NOT_UNIQUE", f"Expected one body named {reference!r}.")
    return matches[0]


def _select(session, entity, *, mark=0, append=False):
    if not session.select(entity, mark=mark, append=append):
        raise CadError("SELECTION_FAILED", "Native entity selection was refused.")


def _edges(session, selector="all", minimum=None):
    edges = session.resolve_edges(selector)
    if minimum is not None:
        minimum = _number(minimum, "skip_shorter_mm")
        if minimum < 0:
            raise CadError("INVALID_ARGUMENT", "skip_shorter_mm must be nonnegative.")
        edges = [e for e in edges if _number(e.GetLength(), "Native edge length") * 1000 >= minimum]
    if not edges:
        raise CadError("NO_EDGES", "No edges match the requested selection.")
    return _distinct(edges)


def _result(session, feature, name=None, **extra):
    if feature is None:
        raise CadError("FEATURE_CREATION_FAILED", "The native API returned no feature.")
    return session.feature_result(binding.wrap(feature, "IFeature"), name=name, **extra)


def _definition(model, kind, interface):
    value = binding.wrap(_manager(model).CreateDefinition(kind), interface)
    if value is None:
        raise CadError("FEATURE_DEFINITION_UNAVAILABLE", f"Native feature definition {kind} is unavailable.")
    return value


def _feature_errors(model):
    failures = []
    for feature, _ in _walk(model):
        code, warning = feature.GetErrorCode2()
        if code and not warning:
            failures.append({"name": str(feature.Name), "code": int(code)})
    return failures


def list_bodies(session):
    model = _model(session, part=True)
    stamp = int(model.GetUpdateStamp())
    result = []
    for index, body in enumerate(_bodies(session), 1):
        mass = _array(body.GetMassProperties(1.0), "body mass properties")
        if len(mass) < 6:
            raise CadError("BODY_PROPERTIES_UNAVAILABLE", "Native body properties are incomplete.")
        box = [_number(v, "Body bound") * 1000 for v in _array(body.GetBodyBox(), "body box")]
        result.append({"id": f"B{stamp}_{index}", "name": str(body.Name),
                       "volume_mm3": _number(mass[3], "Body volume") * 1e9,
                       "bounding_box_mm": box, "bounds_kind": "native_approximate_body_box"})
    return {"ok": True, "count": len(result), "bodies": result}


def combine_bodies(session, operation, main, tools=None, name="Combine"):
    codes = {"add": 15903, "union": 15903, "subtract": 15902, "cut": 15902,
             "intersect": 15901, "common": 15901}
    if operation not in codes:
        raise CadError("INVALID_ARGUMENT", "operation must be add, subtract or intersect.")
    main_body = _body(session, main)
    tool_bodies = [_body(session, value) for value in tools] if tools is not None else [
        body for body in _bodies(session) if not _same(body, main_body)]
    if not tool_bodies or any(_same(main_body, body) for body in tool_bodies) or len(_distinct(tool_bodies)) != len(tool_bodies):
        raise CadError("INVALID_BODY_SET", "Use distinct tool bodies, excluding the main body.")
    # The native CUT signature has a separate target; ADD and INTERSECT require
    # a null target and the complete body set in ToolVar.
    cutting = codes[operation] == 15902
    feature = _manager(_model(session, part=True)).InsertCombineFeature(
        codes[operation], main_body if cutting else None,
        _objects(tool_bodies if cutting else [main_body, *tool_bodies]))
    return _result(session, feature, name, operation=operation, tool_count=len(tool_bodies))


def split_body(session, plane, name="Split"):
    model = _model(session, part=True)
    plane_feature = session.resolve_plane(_text(plane, "plane"))
    model.ClearSelection2(True)
    _select(session, plane_feature)
    manager = _manager(model)
    bodies = _array(manager.PreSplitBody2(), "split preview bodies")
    if len(bodies) < 2:
        raise CadError("SPLIT_DID_NOT_INTERSECT", "The cutting plane did not produce multiple bodies.")
    bodies = [binding.wrap(b, "IBody2") for b in bodies]
    # The preview stage retains the cutting selection. PostSplitBody2 uses its
    # prepared bodies after that selection is cleared, and requires typed
    # dispatch-null origins plus a BSTR filename array, even for unsaved bodies.
    model.ClearSelection2(True)
    native = any(hasattr(body, "_oleobj_") for body in bodies)
    origins = binding.array_dispatch([None] * len(bodies), force=native)
    paths = binding.array_string([""] * len(bodies)) if native else [""] * len(bodies)
    feature = manager.PostSplitBody2(_objects(bodies), False, origins, paths, "")
    return _result(session, feature, name, body_count=len(bodies), external_files_created=False)


def add_thread(session, size, x_mm, y_mm, z_mm, length_mm, internal=False, name="Thread"):
    match = re.fullmatch(r"M\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)", _text(size, "size"))
    if not match or type(internal) is not bool:
        raise CadError("INVALID_ARGUMENT", "Use an explicit metric size such as M10x1.5 and a boolean internal flag.")
    diameter, pitch = map(float, match.groups())
    length = _number(length_mm, "length_mm", positive=True)
    center = [_number(v, "Thread center") / 1000 for v in (x_mm, y_mm, z_mm)]
    expected = diameter - 1.08253175473 * pitch if internal else diameter
    if pitch <= 0 or expected <= 0:
        raise CadError("INVALID_THREAD_SIZE", "Thread diameter and pitch are incompatible.")
    candidates = []
    for edge in _distinct(e for body in _bodies(session) for e in _array(body.GetEdges(), "body edges")):
        edge = binding.wrap(edge, "IEdge")
        curve = binding.wrap(edge.GetCurve(), "ICurve")
        if curve is None or not curve.IsCircle():
            continue
        values = _array(curve.CircleParams, "circle parameters")
        if len(values) == 7 and math.dist(values[:3], center) <= 1e-5 and abs(values[6] * 2000 - expected) <= 0.05:
            candidates.append(edge)
    if len(candidates) != 1:
        raise CadError("THREAD_EDGE_NOT_UNIQUE", "Expected one circular start edge at the supplied center and compatible diameter.")
    model = _model(session, part=True)
    model.ClearSelection2(True)
    _select(session, candidates[0], mark=1)
    definition = _definition(model, 87, "IThreadFeatureData")
    if definition.InitializeThreadData() is False:
        raise CadError("THREAD_PROFILE_UNAVAILABLE", "Native thread-profile initialization failed.")
    definition.Type = "Metric Tap" if internal else "Metric Die"
    definition.Size = size
    definition.Edge = candidates[0]
    definition.EndCondition = 0
    definition.BlindDepth = length / 1000
    definition.RightHanded = True
    definition.ThreadMethod = 0
    feature = _manager(model).CreateFeature(definition)
    return _result(session, feature, name, size=size, pitch_mm=pitch, nominal_diameter_mm=diameter,
                   start_diameter_mm=expected, length_mm=length, internal=internal)


def _vertex_radii(edges, entries, default_radius):
    """Resolve explicit endpoint radii before creating any native feature."""
    requested = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 4:
            raise CadError("INVALID_ARGUMENT", "radii_at_mm entries are [x,y,z,radius].")
        requested.append(([_number(v, "Radius endpoint") / 1000 for v in entry[:3]],
                          _number(entry[3], "Endpoint radius", positive=True) / 1000))
    endpoints, radii, matched = [], [], set()
    for edge in edges:
        start, end = edge.GetStartVertex(), edge.GetEndVertex()
        if start is None or end is None:
            raise CadError("FILLET_ENDPOINT_UNAVAILABLE", "Variable radius requires edges with finite native endpoints.")
        for vertex in (start, end):
            vertex = binding.wrap(vertex, "IVertex")
            point = _array(vertex.GetPoint(), "vertex point")
            choices = [i for i, (at, _) in enumerate(requested) if len(point) == 3 and math.dist(at, point) <= 1e-7]
            if len(choices) > 1:
                raise CadError("FILLET_ENDPOINT_AMBIGUOUS", "Multiple radii target the same endpoint.")
            radius = requested[choices[0]][1] if choices else default_radius
            if choices:
                matched.add(choices[0])
            endpoints.append(vertex)
            radii.append(radius)
    if len(matched) != len(requested):
        raise CadError("FILLET_ENDPOINT_NOT_FOUND", "Every radius control must match a selected edge endpoint.")
    return endpoints, radii


def add_fillet(session, radius_mm, edges="all", name="Fillet", radii_at_mm=None,
               skip_shorter_mm=None, tangent_propagation=False):
    radius = _number(radius_mm, "radius_mm", positive=True) / 1000
    if type(tangent_propagation) is not bool:
        raise CadError("INVALID_ARGUMENT", "tangent_propagation must be boolean.")
    chosen = _edges(session, edges, skip_shorter_mm)
    controls = None
    if radii_at_mm is not None:
        if not isinstance(radii_at_mm, list) or not radii_at_mm:
            raise CadError("INVALID_ARGUMENT", "radii_at_mm must be a nonempty endpoint-radius list.")
        controls = _vertex_radii(chosen, radii_at_mm, radius)
    model = _model(session, part=True)
    model.ClearSelection2(True)
    for index, edge in enumerate(chosen):
        _select(session, edge, mark=1, append=index > 0)
    if controls is None:
        definition = _definition(model, 1, "ISimpleFilletFeatureData2")
        if definition.Initialize(0) is False:
            raise CadError("FILLET_DEFINITION_FAILED", "Cannot initialize constant-radius fillet.")
        definition.DefaultRadius = radius
        definition.ConicTypeForCrossSectionProfile = 0
        definition.OverflowType = 0
        definition.PropagateToTangentFaces = tangent_propagation
        feature = _manager(model).CreateFeature(definition)
        return _result(session, feature, name, edge_count=len(chosen), radius_mm=radius_mm)
    vertices, values = controls
    # Every endpoint is an explicit native control reference; duplicate shared
    # vertices are selected once and carry one consistent radius.
    unique = []
    for vertex, value in zip(vertices, values):
        prior = next((r for v, r in unique if _same(v, vertex)), None)
        if prior is not None and abs(prior - value) > 1e-12:
            raise CadError("FILLET_ENDPOINT_CONFLICT", "One shared vertex cannot have two radii.")
        if prior is None:
            unique.append((vertex, value))
    for vertex, _ in unique:
        _select(session, vertex, mark=256, append=True)
    options = 4 | (1 if tangent_propagation else 0)
    feature = _manager(model).FeatureFillet3(options, 0., 0., 0., 1, 0, 0,
        _doubles([radius] * len(chosen)), None, None, None, _doubles([r for _, r in unique]), None, None)
    return _result(session, feature, name, edge_count=len(chosen), variable_radius=True,
                   vertex_radii=[{"point_mm": [v * 1000 for v in vertex.GetPoint()], "radius_mm": r * 1000}
                                 for vertex, r in unique])


def _normal(face):
    surface = binding.wrap(face.GetSurface(), "ISurface")
    if surface is None or not surface.IsPlane():
        return None
    raw = _array(face.Normal, "face normal")
    if len(raw) != 3:
        return None
    length = math.sqrt(sum(_number(v, "Face normal") ** 2 for v in raw))
    return [v / length for v in raw] if length > 1e-12 else None


def _center(face):
    box = _array(face.GetBox(), "face box")
    if len(box) != 6:
        raise CadError("FACE_GEOMETRY_UNAVAILABLE", "Native face bounds are unavailable.")
    return [(box[i] + box[i + 3]) / 2 for i in range(3)]


def add_full_round(session, face, x_mm, y_mm, z_mm, name="FullRound"):
    center_face = session.resolve_face(face, point_mm=[x_mm, y_mm, z_mm])
    direction = _normal(center_face)
    if direction is None:
        raise CadError("PLANAR_FACE_REQUIRED", "The central full-round face must be planar.")
    adjacent = _distinct(binding.wrap(f, "IFace2") for edge in _array(center_face.GetEdges(), "face edges")
                         for f in _array(binding.wrap(edge, "IEdge").GetTwoAdjacentFaces2(), "adjacent faces")
                         if f is not None and not _same(f, center_face))
    pairs = []
    for a, b in combinations(adjacent, 2):
        an, bn = _normal(a), _normal(b)
        if an is None or bn is None or abs(sum(x * y for x, y in zip(an, bn))) < .999999:
            continue
        if abs(sum(x * y for x, y in zip(an, direction))) > 1e-6:
            continue
        distance = abs(sum(n * (p - q) for n, p, q in zip(an, _center(a), _center(b))))
        if distance > 1e-9:
            pairs.append((distance, a, b))
    pairs.sort(key=lambda value: value[0])
    if not pairs or len(pairs) > 1 and math.isclose(pairs[0][0], pairs[1][0], rel_tol=1e-6, abs_tol=1e-9):
        raise CadError("FULL_ROUND_SIDES_AMBIGUOUS", "A unique closest pair of opposite planar side faces is required.")
    width, a, b = pairs[0]
    model = _model(session, part=True)
    model.ClearSelection2(True)
    for entity, mark in ((a, 2), (center_face, 512), (b, 4)):
        _select(session, entity, mark=mark, append=mark != 2)
    definition = _definition(model, 1, "ISimpleFilletFeatureData2")
    if definition.Initialize(3) is False:
        raise CadError("FILLET_DEFINITION_FAILED", "Cannot initialize full-round fillet.")
    definition.ConicTypeForCrossSectionProfile = 0
    definition.SetFaces(3, _objects([a]))
    definition.SetFaces(4, _objects([center_face]))
    definition.SetFaces(5, _objects([b]))
    return _result(session, _manager(model).CreateFeature(definition), name, width_mm=width * 1000)


def add_chamfer(session, distance_mm, edges="all", name="Chamfer", angle_deg=45., from_face=None):
    distance = _number(distance_mm, "distance_mm", positive=True) / 1000
    angle = _number(angle_deg, "angle_deg")
    if not 0 < angle < 90:
        raise CadError("INVALID_ARGUMENT", "angle_deg must be strictly between zero and ninety.")
    chosen = _edges(session, edges)
    reference = session.resolve_face(from_face) if from_face is not None else None
    if reference is not None:
        adjacent = _array(reference.GetEdges(), "reference face edges")
        if any(not any(_same(edge, candidate) for candidate in adjacent) for edge in chosen):
            raise CadError("CHAMFER_REFERENCE_MISMATCH", "Each selected edge must belong to from_face.")
    model = _model(session, part=True)
    model.ClearSelection2(True)
    for i, edge in enumerate(chosen):
        _select(session, edge, append=i > 0)
    if reference is not None:
        _select(session, reference, mark=1, append=True)
    feature = _manager(model).InsertFeatureChamfer(4, 1, distance, math.radians(angle), 0., 0., 0., 0.)
    return _result(session, feature, name, edge_count=len(chosen), distance_mm=distance_mm, angle_deg=angle)


def _count(value):
    if type(value) is not int or not 2 <= value <= 10000:
        raise CadError("INVALID_ARGUMENT", "Pattern count must be an integer from 2 to 10000.")
    return value


def add_linear_pattern(session, count, spacing_mm, direction="+x", feature_name=None):
    count = _count(count)
    spacing = _number(spacing_mm, "spacing_mm", positive=True) / 1000
    if direction not in {"x", "+x", "-x", "y", "+y", "-y", "z", "+z", "-z"}:
        raise CadError("INVALID_ARGUMENT", "direction must be a signed world axis.")
    seed = _feature(session, feature_name) if feature_name else _last_solid_feature(session)
    plane = session.resolve_plane({"x": "right", "y": "top", "z": "front"}[direction[-1]])
    model = _model(session, part=True)
    model.ClearSelection2(True)
    _select(session, plane, mark=1)
    _select(session, seed, mark=4, append=True)
    definition = _definition(model, 6, "ILinearPatternFeatureData")
    definition.D1Axis = plane.GetSpecificFeature2()
    definition.D1Spacing = spacing
    definition.D1TotalInstances = count
    definition.D1ReverseDirection = direction.startswith("-")
    definition.D2TotalInstances = 1
    definition.GeometryPattern = False
    definition.PatternFeatureArray = _objects([seed])
    return _result(session, _manager(model).CreateFeature(definition), None,
                   seed=str(seed.Name), instances=count, direction=direction, spacing_mm=spacing_mm)


def add_circular_pattern(session, count, center_x_mm, center_y_mm, feature_name=None):
    count = _count(count)
    x, y = _number(center_x_mm, "center_x_mm"), _number(center_y_mm, "center_y_mm")
    seed = _feature(session, feature_name) if feature_name else _last_solid_feature(session)
    planes = []
    from . import geometry
    for base, offset in (("right", x), ("top", y)):
        if abs(offset) <= 1e-12:
            planes.append(session.resolve_plane(base))
        else:
            plane_name = "MCP_PatternPlane_" + uuid4().hex[:10]
            geometry.add_plane(session, base=base, offset_mm=offset, name=plane_name)
            planes.append(_feature(session, plane_name))
    model = _model(session, part=True)
    model.ClearSelection2(True)
    for i, plane in enumerate(planes):
        _select(session, plane, append=i > 0)
    if not model.InsertAxis2(True):
        raise CadError("PATTERN_AXIS_FAILED", "Cannot create a Z-axis through the supplied XY center.")
    axis = binding.wrap(model.FeatureByPositionReverse(0), "IFeature")
    if axis is None or str(axis.GetTypeName2()) != "RefAxis":
        raise CadError("PATTERN_AXIS_UNVERIFIED", "Native axis feature could not be identified.")
    model.ClearSelection2(True)
    _select(session, axis, mark=1)
    _select(session, seed, mark=4, append=True)
    definition = _definition(model, 5, "ICircularPatternFeatureData")
    definition.Axis = axis.GetSpecificFeature2()
    definition.TotalInstances = count
    definition.EqualSpacing = True
    definition.Spacing = 2 * math.pi
    definition.ReverseDirection = False
    definition.GeometryPattern = False
    definition.PatternFeatureArray = _objects([seed])
    return _result(session, _manager(model).CreateFeature(definition), None, instances=count,
                   center_mm=[x, y, 0], axis="z", seed=str(seed.Name))


def add_mirror(session, plane, offset_mm=0., features=None, name="Mirror"):
    offset = _number(offset_mm, "offset_mm")
    seeds = [_feature(session, value) for value in features] if features else [_last_solid_feature(session)]
    if len(_distinct(seeds)) != len(seeds):
        raise CadError("INVALID_ARGUMENT", "Mirror seed features must be unique.")
    if abs(offset) > 1e-12:
        from . import geometry
        plane_name = "MCP_MirrorPlane_" + uuid4().hex[:10]
        geometry.add_plane(session, base=plane, offset_mm=offset, name=plane_name)
        mirror_plane = _feature(session, plane_name)
    else:
        mirror_plane = session.resolve_plane(plane)
    model = _model(session, part=True)
    model.ClearSelection2(True)
    _select(session, mirror_plane, mark=2)
    for seed in seeds:
        _select(session, seed, mark=1, append=True)
    # Mirror data edits existing mirrors. Native creation consumes marked
    # features (1) and the reflection plane (2) through InsertMirrorFeature2.
    created = _manager(model).InsertMirrorFeature2(False, False, False, False, 0)
    return _result(session, created, name,
                   seeds=[str(f.Name) for f in seeds], plane=str(mirror_plane.Name))


def add_shell(session, thickness_mm, open_face="+z"):
    thickness = _number(thickness_mm, "thickness_mm", positive=True) / 1000
    refs = [] if open_face == "none" else open_face if isinstance(open_face, list) else [open_face]
    if not isinstance(refs, list) or len(refs) > 128:
        raise CadError("INVALID_ARGUMENT", "open_face must be a face reference, list, or 'none'.")
    faces = [session.resolve_face(reference) for reference in refs]
    if len(_distinct(faces)) != len(faces):
        raise CadError("INVALID_ARGUMENT", "Shell opening faces must be unique.")
    model = _model(session, part=True)
    before = {(str(feature.Name), str(feature.GetTypeName2())) for feature, _ in _walk(model)}
    model.ClearSelection2(True)
    for i, face in enumerate(faces):
        _select(session, face, mark=1, append=i > 0)
    # Shell data can edit an existing shell but CreateDefinition does not create
    # it. InsertFeatureShell is a void native method, so success requires a new
    # actual feature in the model's history and retained native thickness.
    model.InsertFeatureShell(thickness, False)
    created = [feature for feature, parent in _walk(model) if parent is None
               and str(feature.GetTypeName2()) == "Shell"
               and (str(feature.Name), str(feature.GetTypeName2())) not in before]
    if len(created) != 1:
        raise CadError("SHELL_CREATION_UNVERIFIED", "Native shell insertion did not create one new shell feature.")
    definition = binding.wrap(created[0].GetDefinition(), "IShellFeatureData")
    if definition is None or not math.isclose(_number(definition.Thickness, "Native shell thickness"),
                                               thickness, rel_tol=1e-8, abs_tol=1e-10):
        raise CadError("SHELL_THICKNESS_UNVERIFIED", "The new shell did not retain its requested native thickness.")
    return _result(session, created[0], "Shell", thickness_mm=thickness_mm,
                   opened_faces=len(faces))


def _dimension(model, name):
    name = _text(name, "Dimension name")
    dimension = binding.wrap(model.Parameter(name), "IDimension")
    if dimension is None or str(dimension.GetNameForSelection()) != name:
        raise CadError("DIMENSION_NOT_FOUND", "Use the exact dimension name from list_dimensions.")
    native_type = int(dimension.GetType())
    if native_type not in (0, 1):
        raise CadError("DIMENSION_TYPE_UNSUPPORTED", "Only linear and angular double dimensions are supported.")
    return dimension, "mm" if native_type == 0 else "deg"


def _dimension_value(dimension, unit):
    raw = _number(dimension.SystemValue, "Native dimension")
    return raw * 1000 if unit == "mm" else math.degrees(raw)


def set_dimension(session, dimension_name, value_mm):
    value = _number(value_mm, "value_mm")
    model = _model(session)
    dimension, unit = _dimension(model, dimension_name)
    if int(dimension.DrivenState) != 2:
        raise CadError("DIMENSION_NOT_DRIVING", "A driven/reference dimension cannot be assigned.")
    previous = _number(dimension.SystemValue, "Previous native dimension")
    requested = value / 1000 if unit == "mm" else math.radians(value)
    try:
        dimension.SystemValue = requested
        rebuilt = bool(model.EditRebuild3())
        current, _ = _dimension(model, dimension_name)
        actual = _dimension_value(current, unit)
        broken = _feature_errors(model)
        if not rebuilt or broken or not math.isclose(actual, value, rel_tol=1e-10, abs_tol=1e-6):
            raise CadError("DIMENSION_POSTCONDITION_FAILED", "Requested signed value or successful rebuild was not retained.")
    except Exception as exc:
        try:
            dimension, _ = _dimension(model, dimension_name)
            dimension.SystemValue = previous
            restored_rebuild = bool(model.EditRebuild3())
            restored, _ = _dimension(model, dimension_name)
            verified = restored_rebuild and math.isclose(float(restored.SystemValue), previous, rel_tol=1e-10, abs_tol=1e-12)
            rollback = {"attempted": True, "verified": verified}
        except Exception as restore_error:
            rollback = {"attempted": True, "verified": False, "error": str(restore_error)}
        return {"ok": False, "code": getattr(exc, "code", "DIMENSION_ASSIGN_FAILED"), "error": str(exc),
                "dimension": dimension_name, "rollback": rollback, "outcome_may_be_indeterminate": not rollback["verified"]}
    measured = session.get_mass_properties()
    return {**measured, "ok": measured.get("ok") is True, "dimension": dimension_name,
            "previous_value": previous * 1000 if unit == "mm" else math.degrees(previous),
            "value": actual, "unit": unit, "rebuild_ok": rebuilt, "broken_features": broken}


def _feature_dimensions(feature):
    result, seen = [], set()
    display = binding.wrap(feature.GetFirstDisplayDimension(), "IDisplayDimension")
    while display is not None:
        dimension = binding.wrap(display.GetDimension2(0), "IDimension")
        if dimension is None:
            raise CadError("DIMENSION_UNAVAILABLE", "Native display dimension has no parameter.")
        name = str(dimension.GetNameForSelection())
        if name in seen:
            raise CadError("DIMENSION_HISTORY_INVALID", "Display-dimension traversal is cyclic.")
        seen.add(name)
        kind = int(dimension.GetType())
        unit = {0: "mm", 1: "deg"}.get(kind)
        result.append({"name": name, "feature": str(feature.Name), "unit": unit or "unknown",
                       "value": _dimension_value(dimension, unit) if unit else None,
                       "driving": int(dimension.DrivenState) == 2})
        display = binding.wrap(feature.GetNextDisplayDimension(display), "IDisplayDimension")
    return result


def list_dimensions(session, contains=None):
    if contains is not None and not isinstance(contains, str):
        raise CadError("INVALID_ARGUMENT", "contains must be text or null.")
    result, names = [], set()
    for feature, _ in _walk(_model(session)):
        for entry in _feature_dimensions(feature):
            if entry["name"] not in names and (not contains or contains.casefold() in (entry["name"] + entry["feature"]).casefold()):
                names.add(entry["name"])
                result.append(entry)
    return {"ok": True, "count": len(result), "dimensions": result}


def _point_model(session, point, transform):
    if isinstance(point, (tuple, list)):
        values = [_number(v, "Sketch point") for v in point]
    else:
        point = binding.wrap(point, "ISketchPoint")
        values = [_number(getattr(point, axis), "Sketch point") for axis in ("X", "Y", "Z")]
    if len(values) != 3:
        raise CadError("SKETCH_DATA_UNAVAILABLE", "Expected a native XYZ sketch point.")
    utility = binding.wrap(session._ensure().GetMathUtility(), "IMathUtility")
    native = binding.wrap(utility.CreatePoint(_doubles(values)), "IMathPoint")
    converted = binding.wrap(native.MultiplyTransform(transform), "IMathPoint")
    return [_number(v, "Model sketch point") * 1000 for v in _array(converted.ArrayData, "model point")]


def read_sketch(session, name):
    feature = _feature(session, name)
    if str(feature.GetTypeName2()) not in {"ProfileFeature", "3DProfileFeature"}:
        raise CadError("SKETCH_REQUIRED", "The exact feature must be a 2D or 3D sketch.")
    sketch = binding.wrap(feature.GetSpecificFeature2(), "ISketch")
    if sketch is None:
        raise CadError("SKETCH_UNAVAILABLE", "Native sketch interface is unavailable.")
    to_model = binding.wrap(binding.wrap(sketch.ModelToSketchTransform, "IMathTransform").Inverse(), "IMathTransform")
    result = []
    for index, raw in enumerate(_array(sketch.GetSketchSegments(), "sketch segments"), 1):
        segment = binding.wrap(raw, "ISketchSegment")
        kind = int(segment.GetType())
        entry = {"index": index, "type": {0: "line", 1: "arc", 2: "ellipse", 3: "spline", 4: "text", 5: "parabola"}.get(kind, "unknown"),
                 "construction": bool(segment.ConstructionGeometry), "length_mm": _number(segment.GetLength(), "Sketch segment length") * 1000}
        interface = {0: "ISketchLine", 1: "ISketchArc", 2: "ISketchEllipse", 3: "ISketchSpline"}.get(kind)
        if interface:
            specific = binding.wrap(raw, interface)
            if kind in (0, 1, 2):
                entry["start_mm"] = _point_model(session, specific.GetStartPoint2(), to_model)
                entry["end_mm"] = _point_model(session, specific.GetEndPoint2(), to_model)
            if kind in (1, 2):
                entry["center_mm"] = _point_model(session, specific.GetCenterPoint2(), to_model)
            if kind == 1:
                entry.update(radius_mm=_number(specific.GetRadius(), "Arc radius") * 1000, circle=bool(specific.IsCircle()))
            if kind == 2:
                entry["major_mm"] = _point_model(session, specific.GetMajorPoint2(), to_model)
                entry["minor_mm"] = _point_model(session, specific.GetMinorPoint2(), to_model)
            if kind == 3:
                points = _array(specific.GetPoints2(), "spline points")
                if points and all(type(v) in (int, float) for v in points):
                    if len(points) % 3:
                        raise CadError("SKETCH_DATA_UNAVAILABLE", "Spline point coordinates are incomplete.")
                    points = [points[i:i + 3] for i in range(0, len(points), 3)]
                entry["points_mm"] = [_point_model(session, point, to_model) for point in points]
        result.append(entry)
    status = int(sketch.GetConstrainedStatus())
    return {"ok": True, "name": str(feature.Name), "coordinate_system": "model", "unit": "mm", "segments": result,
            "count": len(result), "dimensions": _feature_dimensions(feature), "constraint_status": status,
            "fully_defined": status == 3}


def list_features(session):
    result = []
    for feature, parent in _walk(_model(session)):
        code, warning = feature.GetErrorCode2()
        result.append({"name": str(feature.Name), "type": str(feature.GetTypeName2()), "parent": parent,
                       "suppressed": bool(feature.IsSuppressed()), "error_code": int(code), "warning": bool(warning)})
    return {"ok": True, "count": len(result), "features": result}


def _equation_manager(model):
    manager = binding.wrap(model.GetEquationMgr(), "IEquationMgr")
    if manager is None:
        raise CadError("EQUATIONS_UNAVAILABLE", "Native equation manager is unavailable.")
    return manager


def _equation_lhs(equation):
    if not isinstance(equation, str) or len(equation) > 20000 or any(c in equation for c in "\x00\r\n"):
        raise CadError("INVALID_EQUATION", "An equation must be one finite-length text line.")
    match = re.match(r'^\s*"([^"\r\n]+)"\s*=\s*(\S[\s\S]*)$', equation)
    if not match:
        raise CadError("INVALID_EQUATION", 'Use an explicit quoted left side, e.g. "Width" = 40mm.')
    return match.group(1)


def list_equations(session):
    manager = _equation_manager(_model(session))
    result = []
    count = int(manager.GetCount())
    if not 0 <= count <= 10000:
        raise CadError("EQUATION_COUNT_INVALID", "Native equation count is outside its bound.")
    for index in range(count):
        equation = str(manager.Equation(index))
        value = manager.Value(index)
        finite = type(value) in (int, float) and math.isfinite(float(value))
        status = int(manager.Status)
        result.append({"index": index, "equation": equation, "name": _equation_lhs(equation),
                       "value": float(value) if finite else None, "global": bool(manager.GlobalVariable(index)),
                       "native_status": status, "valid": finite and status != -1})
    return {"ok": True, "count": count, "equations": result}


def _restore_equations(manager, snapshot):
    errors = []
    try:
        while int(manager.GetCount()):
            if int(manager.Delete(int(manager.GetCount()) - 1)) < 0:
                raise RuntimeError("Native equation deletion failed.")
        for equation in snapshot:
            if int(manager.Add2(-1, equation, False)) < 0:
                raise RuntimeError("Native equation restoration was refused.")
        manager.EvaluateAll()
        verified = [str(manager.Equation(i)) for i in range(int(manager.GetCount()))] == snapshot
    except Exception as exc:
        verified = False
        errors.append(str(exc))
    return {"attempted": True, "verified": verified, "errors": errors}


def set_equations(session, equations):
    if not isinstance(equations, list) or not 1 <= len(equations) <= 256:
        raise CadError("INVALID_ARGUMENT", "Supply 1..256 explicit equations.")
    names = [_equation_lhs(equation) for equation in equations]
    if len({name.casefold() for name in names}) != len(names):
        raise CadError("DUPLICATE_EQUATION", "One batch cannot assign the same left side twice.")
    model = _model(session)
    manager = _equation_manager(model)
    snapshot = [str(manager.Equation(i)) for i in range(int(manager.GetCount()))]
    existing = {_equation_lhs(equation).casefold(): i for i, equation in enumerate(snapshot)}
    try:
        for name, equation in zip(names, equations):
            if name.casefold() in existing:
                manager.SetEquation(existing[name.casefold()], equation)
            elif int(manager.Add2(-1, equation, False)) < 0:
                raise CadError("EQUATION_REJECTED", f"Native manager rejected equation for {name!r}.",
                               equation=equation, native_status=int(manager.Status),
                               equation_count=int(manager.GetCount()))
        manager.EvaluateAll()
        rebuilt = bool(model.EditRebuild3())
        after = list_equations(session)
        actual = {entry["name"].casefold(): entry for entry in after["equations"]}
        if not rebuilt or any(name.casefold() not in actual or not actual[name.casefold()]["valid"] for name in names):
            raise CadError("EQUATION_POSTCONDITION_FAILED", "A requested equation or rebuild did not complete validly.")
        broken = _feature_errors(model)
        if broken:
            raise CadError("EQUATION_REBUILD_FAILED", "Equation changes produced failed native features.")
    except Exception as exc:
        rollback = _restore_equations(manager, snapshot)
        try:
            rollback["verified"] = bool(model.EditRebuild3()) and rollback["verified"]
        except Exception as rebuild_error:
            rollback.update(verified=False, rebuild_error=str(rebuild_error))
        return {"ok": False, "code": getattr(exc, "code", "EQUATION_ASSIGN_FAILED"), "error": str(exc),
                "native_details": getattr(exc, "details", {}),
                "rollback": rollback, "outcome_may_be_indeterminate": not rollback["verified"]}
    measured = session.get_mass_properties()
    return {**measured, **after, "ok": measured.get("ok") is True, "changed": names, "rebuild_ok": rebuilt,
            "broken_features": broken}


def set_equation(session, equation):
    return set_equations(session, [equation])


def delete_equation(session, equation):
    model = _model(session)
    manager = _equation_manager(model)
    snapshot = [str(manager.Equation(i)) for i in range(int(manager.GetCount()))]
    if type(equation) is int or isinstance(equation, str) and equation.isdecimal():
        index = int(equation)
        matches = [index] if 0 <= index < len(snapshot) else []
    else:
        wanted = _text(equation, "Equation name").strip('"')
        matches = [i for i, value in enumerate(snapshot) if _equation_lhs(value) == wanted]
    if len(matches) != 1:
        raise CadError("EQUATION_NOT_UNIQUE", "Supply one exact left-side name or list index.")
    index = matches[0]
    if int(manager.Delete(index)) < 0:
        raise CadError("EQUATION_DELETE_FAILED", "Native equation deletion was refused.")
    manager.EvaluateAll()
    rebuilt = bool(model.EditRebuild3())
    broken = _feature_errors(model)
    if int(manager.GetCount()) != len(snapshot) - 1 or not rebuilt or broken:
        rollback = _restore_equations(manager, snapshot)
        model.EditRebuild3()
        return {"ok": False, "code": "EQUATION_DELETE_POSTCONDITION_FAILED", "rollback": rollback,
                "rebuild_ok": rebuilt, "broken_features": broken, "outcome_may_be_indeterminate": not rollback["verified"]}
    measured = session.get_mass_properties()
    return {**measured, "ok": measured.get("ok") is True, "deleted": snapshot[index], "index": index,
            "rebuild_ok": rebuilt, "broken_features": []}


def delete_feature(session, name, with_children=False, with_equations=False, dry_run=False):
    if any(type(v) is not bool for v in (with_children, with_equations, dry_run)):
        raise CadError("INVALID_ARGUMENT", "Delete options must be boolean.")
    feature = _feature(session, name)
    descendants = _array(feature.GetChildren(), "dependent features")
    equations = list_equations(session)["equations"]
    affected_names = {str(feature.Name), *[str(binding.wrap(f, "IFeature").Name) for f in descendants]}
    referenced = [entry for entry in equations if any("@" + value + '"' in entry["equation"] for value in affected_names)]
    plan = {"ok": True, "dry_run": dry_run, "feature": name,
            "children": [str(binding.wrap(f, "IFeature").Name) for f in descendants],
            "referencing_equations": referenced}
    if dry_run:
        return plan
    if descendants and not with_children:
        raise CadError("DEPENDENT_FEATURES_PRESENT", "Review delete_feature(dry_run=true) and explicitly allow dependent deletion.")
    if referenced and not with_equations:
        raise CadError("REFERENCING_EQUATIONS_PRESENT", "Review the referenced equations before allowing their deletion.")
    model = _model(session)
    # Delete equations from largest index down so earlier indices remain stable.
    manager = _equation_manager(model)
    equation_snapshot = [str(manager.Equation(i)) for i in range(int(manager.GetCount()))]
    try:
        for entry in sorted(referenced, key=lambda value: value["index"], reverse=True):
            if int(manager.Delete(entry["index"])) < 0:
                raise CadError("EQUATION_DELETE_FAILED", "A referenced equation could not be removed.")
        model.ClearSelection2(True)
        _select(session, feature)
        if not _extension(model).DeleteSelection2(2 | (1 if with_children else 0)):
            raise CadError("FEATURE_DELETE_FAILED", "The native API refused feature deletion.")
    except Exception as exc:
        rollback = _restore_equations(manager, equation_snapshot) if referenced else {"attempted": False, "verified": True}
        return {**plan, "ok": False, "code": getattr(exc, "code", "FEATURE_DELETE_FAILED"),
                "error": str(exc), "rollback": rollback,
                "outcome_may_be_indeterminate": not rollback["verified"]}
    rebuilt = bool(model.EditRebuild3())
    exists = any(str(f.Name) == name for f, _ in _walk(model))
    broken = _feature_errors(model)
    return {**plan, "ok": rebuilt and not exists and not broken, "deleted": not exists,
            "rebuild_ok": rebuilt, "broken_features": broken,
            "changes_may_remain": not rebuilt or bool(broken)}


def suppress_feature(session, name, suppress=True):
    if type(suppress) is not bool:
        raise CadError("INVALID_ARGUMENT", "suppress must be boolean.")
    feature = _feature(session, name)
    previous = bool(feature.IsSuppressed())
    if previous == suppress:
        return {"ok": True, "name": name, "suppressed": suppress, "changed": False}
    if not feature.SetSuppression2(0 if suppress else 1, 1, None):
        raise CadError("FEATURE_SUPPRESSION_FAILED", "The native suppression operation was refused.")
    rebuilt = bool(_model(session).EditRebuild3())
    actual = bool(_feature(session, name).IsSuppressed())
    broken = _feature_errors(_model(session))
    if not rebuilt or actual != suppress or broken:
        try:
            feature = _feature(session, name)
            feature.SetSuppression2(0 if previous else 1, 1, None)
            restored_rebuild = bool(_model(session).EditRebuild3())
            verified = restored_rebuild and bool(_feature(session, name).IsSuppressed()) == previous
        except Exception:
            verified = False
        return {"ok": False, "code": "FEATURE_SUPPRESSION_POSTCONDITION_FAILED", "name": name,
                "rebuild_ok": rebuilt, "broken_features": broken,
                "rollback": {"attempted": True, "verified": verified},
                "outcome_may_be_indeterminate": not verified}
    return {"ok": True, "name": name, "suppressed": actual,
            "previous_suppressed": previous, "rebuild_ok": rebuilt,
            "broken_features": broken}


def reorder_feature(session, name, before):
    feature, target = _feature(session, name), _feature(session, before)
    if _same(feature, target):
        raise CadError("INVALID_ARGUMENT", "A feature cannot be moved before itself.")
    model = _model(session)
    moved = _extension(model).ReorderFeature2(str(feature.Name), str(target.Name), 2)
    if not moved:
        raise CadError("FEATURE_REORDER_FAILED", "Native history constraints prevented this move.")
    order = [str(f.Name) for f, parent in _walk(model) if parent is None]
    verified = name in order and before in order and order.index(name) < order.index(before)
    rebuilt = bool(model.EditRebuild3())
    broken = _feature_errors(model)
    return {"ok": verified and rebuilt and not broken, "name": name, "before": before,
            "verified": verified, "rebuild_ok": rebuilt, "broken_features": broken}


def set_appearance(session, rgb=None, transparency=None, component=None):
    if rgb is not None and (not isinstance(rgb, list) or len(rgb) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in rgb)):
        raise CadError("INVALID_ARGUMENT", "rgb must be three integers in 0..255.")
    if transparency is not None:
        transparency = _number(transparency, "transparency")
        if not 0 <= transparency <= 1:
            raise CadError("INVALID_ARGUMENT", "transparency must be within zero and one.")
    model = _model(session)
    if component is not None:
        from .topology import component as find_component
        target = find_component(session, component)
    else:
        target = model
    previous = _array(target.MaterialPropertyValues, "appearance")
    values = previous[:] if len(previous) == 9 else [.7, .7, .7, 1., 1., .5, .3125, 0., 0.]
    if rgb is not None:
        values[:3] = [v / 255 for v in rgb]
    if transparency is not None:
        values[7] = transparency
    if rgb is None and transparency is None:
        return {"ok": True, "changed": False, "material_values": previous, "component": component}
    try:
        target.MaterialPropertyValues = _doubles(values) if hasattr(target, "_oleobj_") else values
        actual = _array(target.MaterialPropertyValues, "appearance readback")
        if len(actual) != 9 or not all(math.isclose(_number(a, "Appearance"), b, abs_tol=1e-7) for a, b in zip(actual, values)):
            raise CadError("APPEARANCE_READBACK_FAILED", "Native appearance did not retain the requested values.")
    except Exception as exc:
        try:
            if previous:
                target.MaterialPropertyValues = _doubles(previous) if hasattr(target, "_oleobj_") else previous
            else:
                target.RemoveMaterialProperty2(1, None)
            restored = _array(target.MaterialPropertyValues, "restored appearance")
            verified = restored == previous
        except Exception:
            verified = False
        return {"ok": False, "error": str(exc), "code": getattr(exc, "code", "APPEARANCE_FAILED"),
                "rollback": {"attempted": True, "verified": verified}, "outcome_may_be_indeterminate": not verified}
    model.GraphicsRedraw2()
    return {"ok": True, "changed": True, "component": component, "rgb": [round(v * 255) for v in actual[:3]],
            "transparency": actual[7], "previous_values": previous, "saved": False}


def _custom_material_database(name, density):
    from ..storage import default_workspace
    folder = default_workspace() / "materials"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"sldmat-v3\0{name}\0{density:.17g}".encode()).hexdigest()[:20]
    path = folder / f"native-{digest}.sldmat"
    ET.register_namespace("mstns", "http://www.solidworks.com/sldmaterials")
    root = ET.Element("{http://www.solidworks.com/sldmaterials}materials", {
        "xmlns:msdata": "urn:schemas-microsoft-com:xml-msdata",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xmlns:sldcolorswatch": "http://www.solidworks.com/sldcolorswatch",
        "version": "2008.03"})
    # The installed sldmaterials schema requires a curve collection even when
    # no physical property references a curve. This inert curve adds no physics.
    curve = ET.SubElement(root, "curves", id="curve0")
    ET.SubElement(curve, "point", x="0", y="0")
    ET.SubElement(curve, "point", x="1", y="0")
    classification = ET.SubElement(root, "classification", name="MCP User Materials")
    material = ET.SubElement(classification, "material", name=name, description="User-specified density only")
    physical = ET.SubElement(material, "physicalproperties")
    ET.SubElement(physical, "DENS", displayname="Mass density", value=format(density, ".17g"))
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-16", xml_declaration=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise CadError("MATERIAL_DATABASE_COLLISION", "An existing custom material database differs; refusing to overwrite it.")
    else:
        with path.open("xb") as stream:
            stream.write(data)
    return path


def _folder_key(value):
    return str(Path(value).resolve()).replace("\\", "/").rstrip("/").casefold()


@contextmanager
def _material_database_folder(session, path):
    """Load a native database while restoring the user's exact folder setting."""
    app = session._ensure()
    folder = str(Path(path).resolve().parent)
    if ";" in folder:
        raise CadError("MATERIAL_FOLDER_UNREPRESENTABLE", "A material-library directory cannot contain the native list separator ';'.")
    previous = app.GetUserPreferenceStringValue(28)  # swFileLocationsMaterialDatabases
    if not isinstance(previous, str):
        raise CadError("MATERIAL_FOLDERS_UNAVAILABLE", "The native material database folder setting is unavailable.")
    existing = [entry for entry in previous.split(";") if entry]
    registered = _folder_key(folder) in {_folder_key(entry) for entry in existing}
    if registered:
        app.GetMaterialDatabases()
        yield
        return
    requested = previous + (";" if previous and not previous.endswith(";") else "") + folder
    failure = None
    try:
        # Register restoration before the setter: a COM setter can change its
        # target and still raise, which must not leak a global preference change.
        try:
            applied = app.SetUserPreferenceStringValue(28, requested)
            retained = app.GetUserPreferenceStringValue(28)
            if applied is False or not isinstance(retained, str) or _folder_key(folder) not in {
                    _folder_key(entry) for entry in retained.split(";") if entry}:
                raise CadError("MATERIAL_FOLDER_REGISTRATION_FAILED", "The native material database folder was not retained.")
            # Updating the folder string alone does not refresh the native
            # material catalogue; this query triggers discovery of our file.
            app.GetMaterialDatabases()
        except CadError:
            raise
        except Exception as exc:
            raise CadError("MATERIAL_FOLDER_REGISTRATION_FAILED", str(exc)) from exc
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            app.SetUserPreferenceStringValue(28, previous)
            if app.GetUserPreferenceStringValue(28) != previous:
                raise RuntimeError("Native material directory readback differs from its original setting.")
            app.GetMaterialDatabases()
        except Exception as exc:
            raise CadError("MATERIAL_FOLDER_RESTORATION_FAILED", str(exc),
                           original_error=str(failure) if failure is not None else None,
                           outcome_may_be_indeterminate=True) from exc


def set_material(session, name, database="", density_kg_m3=None):
    name = _text(name, "Material name")
    density = _number(density_kg_m3, "density_kg_m3", positive=True) if density_kg_m3 is not None else None
    _model(session, part=True)
    if density is not None:
        path = _custom_material_database(name, density)
        canonical_name = name
    else:
        candidates = [Path(database)] if database else [Path(p) for p in _array(session._ensure().GetMaterialDatabases(), "material databases")]
        matches = []
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() != ".sldmat" or candidate.stat().st_size > 64 * 1024 * 1024:
                continue
            try:
                tree = ET.parse(candidate)
            except (ET.ParseError, OSError):
                continue
            for element in tree.iter():
                if element.tag.rsplit("}", 1)[-1] == "material" and element.get("name", "").casefold() == name.casefold():
                    matches.append((candidate, element.get("name")))
        if len(matches) != 1:
            raise CadError("MATERIAL_NOT_UNIQUE", f"Expected one installed material named {name!r}; found {len(matches)}. Supply its exact database path.")
        path, canonical_name = matches[0]
    path = path.resolve()
    with _material_database_folder(session, path):
        return _assign_material(session, path, canonical_name, density)


def _assign_material(session, path, canonical_name, density):
    model = _model(session, part=True)
    part = binding.wrap(model, "IPartDoc")
    configuration = binding.wrap(model.GetActiveConfiguration(), "IConfiguration")
    configuration_name = str(configuration.Name)
    read_configuration = "" if int(model.GetConfigurationCount()) == 1 else configuration_name
    previous_raw = part.GetMaterialPropertyName2(read_configuration)
    previous_name = str(previous_raw[0] if isinstance(previous_raw, (tuple, list)) else previous_raw or "")
    previous_database = str(previous_raw[1]) if isinstance(previous_raw, (tuple, list)) and len(previous_raw) > 1 else ""
    previous = [previous_name, previous_database]
    previous_mass = session.get_mass_properties()
    previous_density = previous_mass.get("mass_properties", {}).get("density_kg_m3")
    current = None
    try:
        part.SetMaterialPropertyName2(configuration_name, str(path), canonical_name)
        rebuilt = bool(model.EditRebuild3())
        if not rebuilt:
            raise CadError("MATERIAL_REBUILD_FAILED", "Native rebuild failed after assigning material.")
        current = part.GetMaterialPropertyName2(read_configuration)
        actual_name = current[0] if isinstance(current, (tuple, list)) else current
        if str(actual_name).casefold() != canonical_name.casefold():
            raise CadError("MATERIAL_READBACK_FAILED", "The native engineering material assignment was not retained.")
        measured = session.get_mass_properties()
        actual_density = measured.get("mass_properties", {}).get("density_kg_m3")
        if density is not None and (type(actual_density) not in (int, float) or not math.isclose(actual_density, density, rel_tol=1e-6, abs_tol=1e-6)):
            raise CadError("MATERIAL_DENSITY_UNVERIFIED", "Native mass properties did not confirm the requested density.")
    except Exception as exc:
        try:
            part.SetMaterialPropertyName2(configuration_name, previous_database, previous_name)
            restored_rebuild = bool(model.EditRebuild3())
            restored = part.GetMaterialPropertyName2(read_configuration)
            restored_name = str(restored[0] if isinstance(restored, (tuple, list)) else restored or "")
            verified = restored_rebuild and restored_name == previous_name
            if type(previous_density) in (int, float) and math.isfinite(previous_density):
                current_density = session.get_mass_properties().get("mass_properties", {}).get("density_kg_m3")
                verified = verified and type(current_density) in (int, float) and math.isclose(
                    current_density, previous_density, rel_tol=1e-6, abs_tol=1e-6)
            rollback = {"attempted": True, "verified": verified}
        except Exception as restore_error:
            rollback = {"attempted": True, "verified": False, "error": str(restore_error)}
        return {"ok": False, "code": getattr(exc, "code", "MATERIAL_ASSIGN_FAILED"), "error": str(exc),
                "material": canonical_name, "previous_material": previous, "rollback": rollback,
                "database": str(path), "configuration": configuration_name,
                "read_configuration": read_configuration,
                "assignment_readback": [str(value) for value in current] if isinstance(current, (tuple, list)) else str(current or ""),
                "outcome_may_be_indeterminate": not rollback["verified"]}
    return {**measured, "ok": rebuilt and measured.get("ok") is True, "material": canonical_name,
            "database": str(path), "density_only": density is not None, "rebuild_ok": rebuilt,
            "previous_material": previous, "saved": False}


TOOLS = {name: globals()[name] for name in (
    "list_bodies", "combine_bodies", "split_body", "add_thread", "add_fillet", "add_full_round", "add_chamfer",
    "add_linear_pattern", "add_circular_pattern", "add_mirror", "add_shell", "set_dimension", "list_dimensions",
    "read_sketch", "list_features", "delete_feature", "suppress_feature", "reorder_feature", "set_appearance",
    "set_material", "set_equation", "set_equations", "list_equations", "delete_equation")}
