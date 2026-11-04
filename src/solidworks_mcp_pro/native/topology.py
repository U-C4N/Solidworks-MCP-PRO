"""Document-scoped native entity references and geometric inspection.

References expire when the document/configuration/update stamp changes. Native
objects never leave the owning CAD worker. Component face coordinates are local
to the referenced part; assembly transforms are returned separately.
"""

from __future__ import annotations

import math

from . import binding
from .errors import CadError


MAX_ENTITIES = 100000
AXES = {
    "+x": (1, 0, 0),
    "-x": (-1, 0, 0),
    "+y": (0, 1, 0),
    "-y": (0, -1, 0),
    "+z": (0, 0, 1),
    "-z": (0, 0, -1),
}
ALIASES = {"right": "+x", "left": "-x", "top": "+y", "bottom": "-y", "front": "+z", "back": "-z"}


def number(value, label="value") -> float:
    if type(value) not in (int, float):
        raise CadError("INVALID_ARGUMENT", f"{label} must be a finite number.")
    try:
        value = float(value)
    except OverflowError as exc:
        raise CadError("INVALID_ARGUMENT", f"{label} is outside floating-point range.") from exc
    if not math.isfinite(value):
        raise CadError("INVALID_ARGUMENT", f"{label} must be finite.")
    return value


def vector(value, label="point", length=3) -> list[float]:
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise CadError("INVALID_ARGUMENT", f"{label} must contain {length} numbers.")
    return [number(v, label) for v in value]


def vector_pair(value, label) -> list[float]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return vector(value[0], label) + vector(value[1], label)
    return vector(value, label, 6)


def items(value, label="native array") -> list:
    if value is None:
        return []
    if not isinstance(value, (tuple, list)) or len(value) > MAX_ENTITIES:
        raise CadError("NATIVE_ARRAY_INVALID", f"{label} is not a bounded native array.")
    return list(value)


def _signature(model) -> tuple:
    configuration = binding.wrap(model.GetActiveConfiguration(), "IConfiguration")
    name = str(configuration.Name) if configuration is not None else ""
    return str(model.GetPathName()), str(model.GetTitle()), int(model.GetUpdateStamp()), name


def _cache(session) -> dict:
    model = session._require_model()
    signature = _signature(model)
    cache = getattr(session, "_topology_cache", None)
    if not isinstance(cache, dict) or cache.get("signature") != signature:
        previous_epoch = cache.get("epoch", 0) if isinstance(cache, dict) else 0
        epoch = max(int(getattr(session, "_topology_epoch_counter", 0)), previous_epoch) + 1
        session._topology_epoch_counter = epoch
        cache = {
            "signature": signature,
            "epoch": epoch,
            "faces": {},
            "edges": {},
            "tokens": {},
            "keys": {},
            "counts": {"face": 0, "edge": 0},
        }
        session._topology_cache = cache
    return cache


def _key(model, entity):
    try:
        extension = binding.wrap(model.Extension, "IModelDocExtension")
        data = extension.GetPersistReference3(entity)
        if data:
            return bytes(data)
    except Exception:
        pass
    return id(getattr(entity, "_oleobj_", entity))


def _token(session, entity, kind, component_name=None):
    cache = _cache(session)
    key = (kind, component_name, _key(session._require_model(), entity))
    if key not in cache["keys"]:
        prefix = "F" if kind == "face" else "E"
        cache["counts"][kind] += 1
        token = f"{prefix}{cache['epoch']}_{cache['counts'][kind]}"
        cache["keys"][key] = token
        cache["tokens"][token] = (kind, component_name, entity)
    return cache["keys"][key]


def component(session, name: str):
    if not isinstance(name, str) or not name.strip():
        raise CadError("INVALID_ARGUMENT", "An exact component instance name is required.")
    assembly = session._require_assembly()
    values = [
        binding.wrap(v, "IComponent2") for v in items(assembly.GetComponents(False), "assembly components")
    ]
    matches = [value for value in values if str(value.Name2) == name]
    if len(matches) != 1:
        raise CadError(
            "COMPONENT_NOT_FOUND" if not matches else "COMPONENT_AMBIGUOUS",
            f"Exact component {name!r} is not unique.",
        )
    result = matches[0]
    if int(result.GetSuppression2()) not in (2, 3):
        raise CadError("COMPONENT_NOT_RESOLVED", f"Component {name!r} must already be resolved.")
    return result


def _bodies(session, component_name=None):
    model = session._require_model()
    if int(model.GetType()) == 1:
        if component_name is not None:
            raise CadError("ASSEMBLY_REQUIRED", "A component selector requires an assembly.")
        return [
            (None, binding.wrap(b, "IBody2"))
            for b in items(binding.wrap(model, "IPartDoc").GetBodies2(0, False), "solid bodies")
        ]
    if int(model.GetType()) != 2:
        raise CadError("MODEL_REQUIRED", "Topology queries require a part or assembly.")
    components = (
        [component(session, component_name)]
        if component_name is not None
        else [
            binding.wrap(c, "IComponent2")
            for c in items(session._require_assembly().GetComponents(False), "assembly components")
        ]
    )
    result = []
    for comp in components:
        state = int(comp.GetSuppression2())
        if state == 0:
            continue
        child = binding.wrap(comp.GetModelDoc2(), "IModelDoc2")
        if state not in (2, 3) or child is None:
            raise CadError("COMPONENT_NOT_RESOLVED", f"Component {comp.Name2!r} has unavailable geometry.")
        if int(child.GetType()) == 2:
            continue  # descendants are supplied by GetComponents(False)
        for body in items(comp.GetBodies2(0), "component solid bodies"):
            result.append((comp, binding.wrap(body, "IBody2")))
    return result


def _context(comp, entity, interface):
    if comp is not None and hasattr(comp, "GetCorrespondingEntity"):
        mapped = comp.GetCorrespondingEntity(entity)
        if mapped is None:
            raise CadError(
                "ENTITY_CONTEXT_UNAVAILABLE", "Cannot establish the component instance's entity context."
            )
        entity = mapped
    return binding.wrap(entity, interface)


def _bounds(entity):
    bounds = vector(entity.GetBox(), "native bounding box", 6)
    return {
        "min_mm": [v * 1000 for v in bounds[:3]],
        "max_mm": [v * 1000 for v in bounds[3:]],
        "approximate": True,
    }


def _surface_name(surface):
    for method, label in (
        ("IsPlane", "plane"),
        ("IsCylinder", "cylinder"),
        ("IsCone", "cone"),
        ("IsSphere", "sphere"),
        ("IsTorus", "torus"),
    ):
        try:
            if bool(getattr(surface, method)()):
                return label
        except AttributeError:
            pass
    return "other"


def face_records(session, component=None) -> list[dict]:
    cache = _cache(session)
    if component in cache["faces"]:
        return cache["faces"][component]
    records = []
    seen = set()
    for body_index, (comp, body) in enumerate(_bodies(session, component), 1):
        for raw in items(body.GetFaces(), "body faces"):
            face = _context(comp, raw, "IFace2")
            name = str(comp.Name2) if comp is not None else None
            token = _token(session, face, "face", name)
            if token in seen:
                continue
            seen.add(token)
            surface = binding.wrap(face.GetSurface(), "ISurface")
            record = {
                "id": token,
                "index": len(records) + 1,
                "component": name,
                "body_index": body_index,
                "surface": _surface_name(surface),
                "area_mm2": number(face.GetArea(), "face area") * 1e6,
                "bounds": _bounds(face),
                "frame": "component_local" if name else "model",
                "reference_epoch": cache["epoch"],
            }
            normal = getattr(face, "Normal", None)
            record["normal"] = (
                vector(normal, "face normal")
                if isinstance(normal, (list, tuple)) and len(normal) == 3
                else None
            )
            bounds = record["bounds"]
            record["center_mm"] = [(a + b) / 2 for a, b in zip(bounds["min_mm"], bounds["max_mm"])]
            if record["surface"] == "cylinder":
                values = items(surface.CylinderParams, "cylinder parameters")
                if len(values) == 7:
                    record["radius_mm"] = number(values[6]) * 1000
            records.append(record)
            if len(records) > MAX_ENTITIES:
                raise CadError("TOPOLOGY_LIMIT", "Too many native faces.")
    cache["faces"][component] = records
    return records


def _edge_record(session, edge, comp, index):
    name = str(comp.Name2) if comp is not None else None
    curve = binding.wrap(edge.GetCurve(), "ICurve")
    params = binding.wrap(edge.GetCurveParams3(), "ICurveParamData")
    lower, upper = number(params.UMinValue), number(params.UMaxValue)
    length = number(curve.GetLength3(lower, upper), "edge length") * 1000
    ends = []
    for method in ("GetStartVertex", "GetEndVertex"):
        vertex = binding.wrap(getattr(edge, method)(), "IVertex")
        ends.append(
            [v * 1000 for v in vector(vertex.GetPoint(), "vertex point")] if vertex is not None else None
        )
    kind = "line" if curve.IsLine() else "circle" if curve.IsCircle() else "other"
    record = {
        "id": _token(session, edge, "edge", name),
        "index": index,
        "type": kind,
        "length_mm": length,
        "ends_mm": ends,
        "component": name,
        "frame": "component_local" if name else "model",
    }
    if all(v is not None for v in ends):
        record["midpoint_mm"] = [(a + b) / 2 for a, b in zip(*ends)]
        delta = [b - a for a, b in zip(*ends)]
        norm = math.hypot(*delta)
        record["direction"] = [v / norm for v in delta] if kind == "line" and norm else None
    if kind == "circle":
        values = items(curve.CircleParams, "circle parameters")
        if len(values) == 7:
            record.update(
                center_mm=[number(v) * 1000 for v in values[:3]],
                axis=[number(v) for v in values[3:6]],
                radius_mm=number(values[6]) * 1000,
            )
    return record


def edge_records(
    session, face=None, feature=None, within_mm=None, min_length_mm=None, component=None
) -> list[dict]:
    cache = _cache(session)
    face_keys = None
    if face is not None:
        selected_face = resolve(session, face, kind="face", component=component)
        face_keys = {
            _key(session._require_model(), edge) for edge in items(selected_face.GetEdges(), "face edges")
        }
    values = [
        (comp, edge)
        for comp, body in _bodies(session, component)
        for edge in items(body.GetEdges(), "body edges")
    ]
    feature_keys = None
    if feature is not None:
        native_feature = session.find_feature(feature)
        feature_keys = set()
        for native_face in items(native_feature.GetFaces(), "feature faces"):
            for edge in items(binding.wrap(native_face, "IFace2").GetEdges(), "feature edges"):
                feature_keys.add(_key(session._require_model(), edge))
    bounds = vector_pair(within_mm, "within_mm") if within_mm is not None else None
    minimum = number(min_length_mm, "min_length_mm") if min_length_mm is not None else -math.inf
    if bounds is not None and any(bounds[i] > bounds[i + 3] for i in range(3)):
        raise CadError(
            "INVALID_ARGUMENT", "Each within_mm lower bound must be no greater than its upper bound."
        )
    if min_length_mm is not None and minimum < 0:
        raise CadError("INVALID_ARGUMENT", "min_length_mm cannot be negative.")
    records, seen = [], set()
    for comp, raw in values:
        edge = binding.wrap(raw, "IEdge")
        record = _edge_record(session, edge, comp, len(seen) + 1)
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        native_key = _key(session._require_model(), edge)
        if face_keys is not None and native_key not in face_keys:
            continue
        if feature_keys is not None and native_key not in feature_keys:
            continue
        if record["length_mm"] < minimum:
            continue
        if bounds is not None:
            points = record.get("ends_mm", [])
            if record["type"] == "circle" and "center_mm" in record:
                norm = math.hypot(*record["axis"])
                if norm <= 1e-12:
                    raise CadError("NATIVE_CURVE_INVALID", "Circular edge exposes no axis direction.")
                extents = [
                    record["radius_mm"] * math.sqrt(max(0.0, 1 - (v / norm) ** 2)) for v in record["axis"]
                ]
                points = [
                    [c - e for c, e in zip(record["center_mm"], extents)],
                    [c + e for c, e in zip(record["center_mm"], extents)],
                ]
                record["region_filter_bounds"] = "supporting_circle_conservative_for_arcs"
            elif record["type"] != "line":
                raise CadError(
                    "EDGE_REGION_UNSUPPORTED",
                    "A whole-edge region filter supports lines and conservative circular bounds.",
                )
            if not points or any(
                p is None or any(not bounds[i] <= p[i] <= bounds[i + 3] for i in range(3)) for p in points
            ):
                continue
        records.append(record)
    cache["edges"][component] = records
    return records


def _closest(entity, point):
    raw = entity.GetClosestPointOn(*[v / 1000 for v in point])
    values = items(raw, "closest point")
    if len(values) < 3:
        raise CadError("ENTITY_DISTANCE_UNAVAILABLE", "Native closest-point query returned no point.")
    closest = [number(v) * 1000 for v in values[:3]]
    return math.dist(point, closest)


def resolve(session, reference, kind="face", component=None, point_mm=None):
    if kind not in {"face", "edge"}:
        raise CadError("INVALID_ARGUMENT", "Only face or edge references are supported.")
    cache = _cache(session)
    if hasattr(reference, "_oleobj_") or (
        not isinstance(reference, (str, int, list, tuple, dict)) and reference is not None
    ):
        return binding.wrap(reference, "IFace2" if kind == "face" else "IEdge")
    if isinstance(reference, dict):
        if set(reference) - {"id", "at", "component"}:
            raise CadError("INVALID_ARGUMENT", "Entity dictionaries accept id/at/component only.")
        component = reference.get("component", component)
        point_mm = reference.get("at", point_mm)
        reference = reference.get("id")
    records = (
        face_records(session, component) if kind == "face" else edge_records(session, component=component)
    )
    if isinstance(reference, str) and reference.startswith("#") and reference[1:].isdigit():
        reference = int(reference[1:])
    if isinstance(reference, str) and reference[:1] in {"F", "E"} and "_" in reference:
        token = cache["tokens"].get(reference)
        if token is None:
            raise CadError("STALE_ENTITY_REFERENCE", "Entity ID expired; list the current topology again.")
        if token[0] != kind or component is not None and token[1] != component:
            raise CadError("ENTITY_SCOPE_MISMATCH", "Entity ID belongs to another type or component.")
        return token[2]
    if isinstance(reference, int) and not isinstance(reference, bool):
        if not 1 <= reference <= len(records):
            raise CadError("ENTITY_NOT_FOUND", "Entity index is outside the current inventory.")
        return cache["tokens"][records[reference - 1]["id"]][2]
    if isinstance(reference, (list, tuple)):
        point_mm = vector(reference)
        reference = None
    direction = ALIASES.get(str(reference).lower(), str(reference).lower()) if reference is not None else None
    candidates = records
    if direction in AXES:
        if kind != "face":
            raise CadError("INVALID_ARGUMENT", "Directional face aliases cannot select an edge.")
        axis = AXES[direction]
        candidates = [
            v
            for v in records
            if v.get("normal") is not None and sum(a * b for a, b in zip(v["normal"], axis)) > 1 - 1e-5
        ]
    elif reference is not None:
        raise CadError("ENTITY_NOT_FOUND", f"Unknown exact entity reference {reference!r}.")
    if point_mm is not None:
        point = vector(point_mm)
        ranked = sorted(
            (_closest(cache["tokens"][v["id"]][2], point), i, v) for i, v in enumerate(candidates)
        )
        tolerance = float(getattr(session, "selection_tolerance_mm", 0.05))
        if not ranked or ranked[0][0] > tolerance:
            raise CadError("ENTITY_NOT_FOUND", "No entity passes the explicit point-distance tolerance.")
        tied = [v for distance, _, v in ranked if abs(distance - ranked[0][0]) <= 1e-7]
        candidates = tied
    elif direction in AXES and candidates:
        axis = AXES[direction]
        largest = max(sum(a * b for a, b in zip(v["center_mm"], axis)) for v in candidates)
        candidates = [
            v for v in candidates if abs(sum(a * b for a, b in zip(v["center_mm"], axis)) - largest) < 1e-6
        ]
    if len(candidates) != 1:
        raise CadError(
            "ENTITY_AMBIGUOUS" if candidates else "ENTITY_NOT_FOUND",
            f"Expected one {kind}; found {len(candidates)}. Use a fresh ID or pick point.",
        )
    return cache["tokens"][candidates[0]["id"]][2]


def select(session, reference, kind, append=False, mark=0, component=None, point_mm=None):
    entity = resolve(session, reference, kind=kind, component=component, point_mm=point_mm)
    selection = binding.wrap(session._require_model().SelectionManager, "ISelectionMgr")
    data = binding.wrap(selection.CreateSelectData(), "ISelectData")
    data.Mark = int(mark)
    selectable = binding.wrap(entity, "IEntity")
    if not selectable.Select4(bool(append), data):
        raise CadError("SELECTION_FAILED", "Native entity selection was refused.")
    return entity


def resolve_edges(session, edges=None, **filters):
    if isinstance(edges, str) and edges.startswith("feature:"):
        filters["feature"] = edges.split(":", 1)[1]
        edges = "all"
    if isinstance(edges, str) and edges.endswith(":outline"):
        face = resolve(session, edges.rsplit(":", 1)[0], kind="face", component=filters.get("component"))
        loops = [binding.wrap(v, "ILoop2") for v in items(face.GetLoops(), "face loops")]
        outer = [loop for loop in loops if loop.IsOuter()]
        if len(outer) != 1:
            raise CadError("OUTLINE_AMBIGUOUS", "Expected one native outer face loop.")
        result = [binding.wrap(v, "IEdge") for v in items(outer[0].GetEdges(), "outer loop edges")]
        minimum = filters.get("skip_shorter_mm", filters.get("min_length_mm"))
        if minimum is not None:
            threshold = number(minimum, "minimum edge length")
            result = [
                edge for edge in result if _edge_record(session, edge, None, 0)["length_mm"] >= threshold
            ]
        if not result:
            raise CadError("ENTITY_NOT_FOUND", "The outer loop has no matching native edges.")
        return result
    minimum = filters.pop("skip_shorter_mm", filters.pop("min_length_mm", None))
    records = edge_records(session, min_length_mm=minimum, **filters)
    if edges is None or edges == "all":
        chosen = records
    elif isinstance(edges, str) and edges.lower() in {"x", "y", "z", "vertical", "horizontal"}:
        label = edges.lower()
        axis = {"x": 0, "y": 1, "z": 2, "vertical": 2}.get(label)
        chosen = [
            r
            for r in records
            if r.get("direction") is not None
            and (abs(r["direction"][axis]) > 1 - 1e-6 if axis is not None else abs(r["direction"][2]) < 1e-6)
        ]
    else:
        values = (
            [v.strip() for v in edges.split(",")]
            if isinstance(edges, str)
            else edges
            if isinstance(edges, (list, tuple))
            else [edges]
        )
        resolved = [
            resolve(
                session,
                int(v) if isinstance(v, str) and v.isdigit() else v,
                kind="edge",
                component=filters.get("component"),
            )
            for v in values
        ]
        result, seen = [], set()
        for entity in resolved:
            key = _key(session._require_model(), entity)
            if key not in seen:
                result.append(entity)
                seen.add(key)
        return result
    if not chosen:
        raise CadError("ENTITY_NOT_FOUND", "No edges match the requested selector.")
    cache = _cache(session)
    return [cache["tokens"][v["id"]][2] for v in chosen]


def list_faces(session, component=None):
    faces = face_records(session, component)
    return {"ok": True, "count": len(faces), "faces": faces, "reference_epoch": _cache(session)["epoch"]}


def list_edges(session, face=None, feature=None, within_mm=None, min_length_mm=None):
    edges = edge_records(session, face, feature, within_mm, min_length_mm)
    return {"ok": True, "count": len(edges), "edges": edges, "reference_epoch": _cache(session)["epoch"]}


TOOLS = {"list_faces": list_faces, "list_edges": list_edges}
