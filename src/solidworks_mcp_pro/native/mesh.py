"""Independent mesh sections and sampled native swept-region geometry.

STL coordinates are millimetres. 3MF model units and build transforms are
honoured. Native CAD tessellation is graphics data, never certified machining
geometry; every approximate comparison and sampled envelope is labelled.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET
import zipfile

from . import binding
from .errors import CadError
from .topology import _bodies, items, number, vector
from .topology import component as find_component


MAX_TRIANGLES = 200000
MAX_BYTES = 64 * 1024 * 1024
UNIT_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "meter": 1000.0,
    "inch": 25.4,
    "foot": 304.8,
}


def _point(value):
    point = vector(value, "mesh point")
    if any(abs(v) > 1e12 for v in point):
        raise CadError("MESH_COORDINATE_LIMIT", "Mesh coordinates exceed the supported range.")
    return tuple(point)


def _apply(transform, point):
    return tuple(sum(point[j] * transform[j * 3 + i] for j in range(3)) + transform[9 + i] for i in range(3))


def _matrix(text):
    if text is None:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    try:
        values = [float(v) for v in text.split()]
    except ValueError as exc:
        raise CadError("INVALID_3MF_TRANSFORM", "A 3MF transform is not numeric.") from exc
    if len(values) != 12 or not all(math.isfinite(v) for v in values):
        raise CadError("INVALID_3MF_TRANSFORM", "A 3MF transform must contain twelve finite values.")
    return values


def _children(element, local):
    return [e for e in element if e.tag.rsplit("}", 1)[-1] == local]


def _one(element, local):
    values = _children(element, local)
    if len(values) != 1:
        raise CadError("INVALID_3MF", f"Expected one {local} element.")
    return values[0]


def read_3mf(path):
    if Path(path).stat().st_size > MAX_BYTES:
        raise CadError("MESH_SIZE_LIMIT", "The 3MF archive is too large.")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(v.file_size for v in entries) > MAX_BYTES:
                raise CadError("MESH_SIZE_LIMIT", "The expanded 3MF package exceeds the inspection budget.")
            names = set()
            for entry in entries:
                if (
                    entry.filename in names
                    or entry.filename.startswith("/")
                    or ".." in PurePosixPath(entry.filename).parts
                    or "\\" in entry.filename
                ):
                    raise CadError("INVALID_3MF_ARCHIVE", "Unsafe or duplicate 3MF package paths.")
                names.add(entry.filename)
            models = [v for v in names if v.lower().endswith(".model")]
            name = (
                "3D/3dmodel.model" if "3D/3dmodel.model" in names else models[0] if len(models) == 1 else None
            )
            if name is None:
                raise CadError("INVALID_3MF", "A unique primary 3MF model is required.")
            raw = archive.read(name)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise CadError("INVALID_3MF_ARCHIVE", "Cannot read the primary 3MF model.") from exc
    if b"<!DOCTYPE" in raw.replace(b"\x00", b"").upper() or b"<!ENTITY" in raw.replace(b"\x00", b"").upper():
        raise CadError("INVALID_3MF_XML", "DTD/entity declarations are not accepted.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CadError("INVALID_3MF_XML", "The primary 3MF model is malformed XML.") from exc
    factor = UNIT_MM.get(root.get("unit", "millimeter"))
    if factor is None:
        raise CadError("3MF_UNIT_UNSUPPORTED", "The 3MF model uses an unsupported length unit.")
    resources = _one(root, "resources")
    objects = {}
    for value in _children(resources, "object"):
        key = value.get("id")
        if not key or key in objects:
            raise CadError("INVALID_3MF", "Object IDs must be unique.")
        objects[key] = value
    cache = {}

    def resolve_object(key, parents=()):
        if key in parents or len(parents) > 64:
            raise CadError("INVALID_3MF", "3MF components contain a cycle or excessive nesting.")
        if key in cache:
            return cache[key]
        if key not in objects:
            raise CadError("INVALID_3MF", "A component references a missing object.")
        obj = objects[key]
        meshes = _children(obj, "mesh")
        components = _children(obj, "components")
        if len(meshes) + len(components) != 1:
            raise CadError("INVALID_3MF", "An object must contain one mesh or component collection.")
        result = []
        if meshes:
            vertices = []
            for v in _children(_one(meshes[0], "vertices"), "vertex"):
                try:
                    vertices.append(_point([float(v.attrib[a]) for a in "xyz"]))
                except (ValueError, KeyError) as exc:
                    raise CadError("INVALID_3MF", "A mesh vertex is not a finite XYZ point.") from exc
            for triangle in _children(_one(meshes[0], "triangles"), "triangle"):
                try:
                    indices = [int(triangle.attrib[a]) for a in ("v1", "v2", "v3")]
                    if any(v < 0 or v >= len(vertices) for v in indices):
                        raise ValueError
                    result.append(tuple(vertices[v] for v in indices))
                except (ValueError, KeyError) as exc:
                    raise CadError("INVALID_3MF", "A triangle references an invalid vertex index.") from exc
        else:
            for value in _children(components[0], "component"):
                if any(k.rsplit("}", 1)[-1] == "path" for k in value.attrib):
                    raise CadError(
                        "3MF_EXTERNAL_MODEL_UNSUPPORTED",
                        "External-model component references must be flattened before inspection.",
                    )
                transform = _matrix(value.get("transform"))
                for triangle in resolve_object(value.get("objectid"), (*parents, key)):
                    result.append(tuple(_point(_apply(transform, p)) for p in triangle))
        if len(result) > MAX_TRIANGLES:
            raise CadError("MESH_TRIANGLE_LIMIT", "3MF geometry exceeds the inspection budget.")
        cache[key] = result
        return result

    result = []
    for value in _children(_one(root, "build"), "item"):
        transform = _matrix(value.get("transform"))
        for triangle in resolve_object(value.get("objectid")):
            result.append(tuple(_point([v * factor for v in _apply(transform, p)]) for p in triangle))
            if len(result) > MAX_TRIANGLES:
                raise CadError("MESH_TRIANGLE_LIMIT", "3MF build instances exceed the inspection budget.")
    if not result:
        raise CadError("MESH_EMPTY", "The 3MF build contains no triangles.")
    return result


def read_mesh(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise CadError("MESH_NOT_FOUND", "Use an existing local mesh file.")
    if path.suffix.lower() == ".3mf":
        return read_3mf(path)
    if path.suffix.lower() != ".stl":
        raise CadError("MESH_FORMAT_UNSUPPORTED", "Mesh inspection accepts STL or 3MF files.")
    from ..visualization import _read_stl

    return _read_stl(path)


def _from_flat(raw):
    values = items(raw, "native tessellation")
    if not values or len(values) % 9 or len(values) > MAX_TRIANGLES * 9:
        raise CadError(
            "TESSELLATION_UNAVAILABLE", "Native graphics triangles are missing, invalid or exceed the budget."
        )
    coordinates = [number(v, "tessellation coordinate") * 1000 for v in values]
    return [
        tuple(tuple(coordinates[i + j : i + j + 3]) for j in (0, 3, 6)) for i in range(0, len(coordinates), 9)
    ]


def native_triangles(session, component_name=None):
    from .assembly import _transform_data, transform_point

    model = session._require_model()
    if int(model.GetType()) == 1 and component_name is None:
        part = binding.wrap(model, "IPartDoc")
        raw = part.GetTessTriangles(True)
        if raw is not None:
            return _from_flat(raw)
    result = []
    for comp, body in _bodies(session, component_name):
        transform = _transform_data(comp) if comp is not None else None
        for face in items(body.GetFaces(), "body faces"):
            triangles = _from_flat(binding.wrap(face, "IFace2").GetTessTriangles(True))
            if transform is not None:
                triangles = [
                    tuple(
                        tuple(v * 1000 for v in transform_point(transform, [x / 1000 for x in p])) for p in t
                    )
                    for t in triangles
                ]
            result.extend(triangles)
            if len(result) > MAX_TRIANGLES:
                raise CadError("MESH_TRIANGLE_LIMIT", "Native tessellation exceeds the inspection budget.")
    if not result:
        raise CadError(
            "TESSELLATION_UNAVAILABLE", "No native triangles were available for the requested solid geometry."
        )
    return result


def _axis(axis):
    if axis not in {"x", "y", "z"}:
        raise CadError("INVALID_ARGUMENT", "axis must be x, y or z.")
    i = "xyz".index(axis)
    return i, [j for j in range(3) if j != i]


def _heights(values):
    if not isinstance(values, (tuple, list)) or len(values) > 256:
        raise CadError("INVALID_ARGUMENT", "heights_mm must contain at most 256 explicit heights.")
    return [number(v, "section height") for v in values]


def _polygon_area(points):
    return abs(
        sum(
            points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1]
            for i in range(len(points))
        )
        / 2
    )


def _inside(point, polygon):
    inside = False
    x, y = point
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def _loops(segments):
    graph = defaultdict(set)
    for a, b in segments:
        graph[a].add(b)
        graph[b].add(a)
    if any(len(v) != 2 for v in graph.values()):
        return [], False
    remaining = {frozenset((a, b)) for a, b in segments}
    loops = []
    while remaining:
        edge = next(iter(remaining))
        start, current = sorted(edge)
        loop = [start]
        previous = start
        remaining.remove(edge)
        while current != start:
            loop.append(current)
            options = graph[current] - {previous}
            if len(options) != 1:
                return [], False
            following = next(iter(options))
            edge = frozenset((current, following))
            if edge not in remaining:
                return [], False
            remaining.remove(edge)
            previous, current = current, following
        loops.append(loop)
    return loops, True


def sections(triangles, axis, heights_mm, tolerance_mm=1e-7):
    index, plane = _axis(axis)
    heights = _heights(heights_mm)
    tolerance = number(tolerance_mm, "section tolerance")
    if tolerance <= 0:
        raise CadError("INVALID_ARGUMENT", "Section tolerance must be positive.")
    scale = max((abs(v) for t in triangles for p in t for v in p), default=1.0)
    quantum = max(tolerance, scale * 1e-12)

    def rounded(point):
        return tuple(round(v / quantum) * quantum for v in point)

    result = []
    for height in heights:
        coplanar = Counter()
        ordinary = set()
        for triangle in triangles:
            signed = [p[index] - height for p in triangle]
            if all(abs(v) <= quantum for v in signed):
                for i in range(3):
                    a, b = [rounded([triangle[j][k] for k in plane]) for j in (i, (i + 1) % 3)]
                    if a != b:
                        coplanar[tuple(sorted((a, b)))] += 1
                continue
            points = []
            for i in range(3):
                j = (i + 1) % 3
                a, b = triangle[i], triangle[j]
                da, db = signed[i], signed[j]
                if abs(da) <= quantum:
                    points.append(rounded([a[k] for k in plane]))
                if da * db < 0 and abs(da) > quantum and abs(db) > quantum:
                    f = da / (da - db)
                    points.append(rounded([a[k] + f * (b[k] - a[k]) for k in plane]))
            points = list(dict.fromkeys(points))
            if len(points) == 2 and points[0] != points[1]:
                ordinary.add(tuple(sorted(points)))
        edges = ordinary | {edge for edge, count in coplanar.items() if count == 1}
        loops, closed = _loops(edges)
        area = None
        if closed:
            area = 0.0
            for i, loop in enumerate(loops):
                depth = sum(_inside(loop[0], other) for j, other in enumerate(loops) if j != i)
                area += (-1 if depth % 2 else 1) * _polygon_area(loop)
        result.append(
            {
                "height_mm": height,
                "segments_mm": [[list(a), list(b)] for a, b in sorted(edges)],
                "loops_mm": [[list(p) for p in loop] for loop in loops],
                "closed": closed,
                "area_mm2": area,
                "perimeter_mm": sum(math.dist(a, b) for a, b in edges),
                "merge_tolerance_mm": quantum,
                "plane_axes": ["xyz"[i] for i in plane],
            }
        )
    return result


def slice_mesh(session, path, axis, heights_mm, frame="object"):
    if frame not in {"object", "model", "world"}:
        raise CadError(
            "INVALID_ARGUMENT",
            "Mesh section frame is authored object/model/world coordinates; no automatic alignment is applied.",
        )
    _axis(axis)
    _heights(heights_mm)
    triangles = read_mesh(path)
    return {
        "ok": True,
        "path": str(Path(path).resolve()),
        "axis": axis,
        "frame": frame,
        "units": "mm",
        "triangle_count": len(triangles),
        "sections": sections(triangles, axis, heights_mm),
        "method": "triangle_plane_intersections",
        "alignment": "authored_coordinates",
    }


def _point_segment_distance(point, a, b):
    d = [b[i] - a[i] for i in range(2)]
    norm = sum(v * v for v in d)
    fraction = max(0.0, min(1.0, sum((point[i] - a[i]) * d[i] for i in range(2)) / norm)) if norm else 0.0
    return math.dist(point, [a[i] + fraction * d[i] for i in range(2)])


def _sample_distance(first, second):
    if not first and not second:
        return 0.0
    if not first or not second:
        return None
    samples = [
        [(1 - f) * a[i] + f * b[i] for i in range(2)] for a, b in first for f in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    if len(samples) * len(second) > 2000000:
        raise CadError(
            "COMPARISON_BUDGET_EXCEEDED", "Section boundary comparison exceeds the explicit sampling budget."
        )
    return max(min(_point_segment_distance(p, a, b) for a, b in second) for p in samples)


def compare_with_mesh(session, path, axis, heights_mm, frame="object", offset_mm=None):
    if frame not in {"object", "model", "world"}:
        raise CadError(
            "INVALID_ARGUMENT",
            "Comparison uses authored model coordinates and an explicit optional mesh translation.",
        )
    _axis(axis)
    _heights(heights_mm)
    offset = vector(offset_mm, "offset_mm") if offset_mm is not None else [0.0, 0.0, 0.0]
    reference = [tuple(tuple(p[i] + offset[i] for i in range(3)) for p in t) for t in read_mesh(path)]
    current = native_triangles(session)
    current_sections = sections(current, axis, heights_mm)
    reference_sections = sections(reference, axis, heights_mm)
    comparisons = []
    for native, mesh in zip(current_sections, reference_sections):
        one = _sample_distance(native["segments_mm"], mesh["segments_mm"])
        two = _sample_distance(mesh["segments_mm"], native["segments_mm"])
        native_area, mesh_area = native["area_mm2"], mesh["area_mm2"]
        comparisons.append(
            {
                "height_mm": native["height_mm"],
                "cad": native,
                "mesh": mesh,
                "area_difference_mm2": native_area - mesh_area
                if native_area is not None and mesh_area is not None
                else None,
                "sampled_boundary_distance_mm": max(one, two)
                if one is not None and two is not None
                else None,
            }
        )
    return {
        "ok": True,
        "axis": axis,
        "frame": frame,
        "mesh_translation_mm": offset,
        "comparisons": comparisons,
        "method": "native_graphics_and_mesh_sections",
        "exact_geometry_equivalence_verified": False,
        "limitations": [
            "CAD triangles are native graphics tessellation, not controlled machining facets.",
            "Boundary distance samples vertices and quarter points; it is not an exact Hausdorff certificate.",
        ],
    }


def convex_hull(points):
    points = sorted(set(tuple(p) for p in points))
    if len(points) <= 1:
        return points

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    halves = []
    for sequence in (points, reversed(points)):
        half = []
        for point in sequence:
            while len(half) >= 2 and cross(half[-2], half[-1], point) <= 0:
                half.pop()
            half.append(point)
        halves.append(half)
    return halves[0][:-1] + halves[1][:-1]


def offset_hull(polygon, margin):
    if margin == 0 or len(polygon) < 3:
        return polygon
    lines = []
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        normal = (dy / length, -dx / length)
        lines.append((normal, normal[0] * a[0] + normal[1] * a[1] + margin))
    result = []
    for i, (a, ad) in enumerate(lines):
        b, bd = lines[i - 1]
        determinant = a[0] * b[1] - a[1] * b[0]
        if abs(determinant) < 1e-12:
            raise CadError("ENVELOPE_OFFSET_DEGENERATE", "Cannot offset a degenerate section hull.")
        result.append(((ad * b[1] - a[1] * bd) / determinant, (a[0] * bd - ad * b[0]) / determinant))
    return result


def _inverse_frame(values):
    columns = [values[i : i + 3] for i in (0, 3, 6)]
    if abs(values[12] - 1) > 1e-8 or any(
        abs(sum(a * b for a, b in zip(columns[i], columns[j])) - (1 if i == j else 0)) > 1e-8
        for i in range(3)
        for j in range(3)
    ):
        raise CadError("FRAME_TRANSFORM_UNSUPPORTED", "A sampled region frame must be rigid and unit-scale.")
    rotation = [values[j * 3 + i] for i in range(3) for j in range(3)]
    translation = [-sum(values[i * 3 + j] * values[9 + j] for j in range(3)) for i in range(3)]
    return [*rotation, *translation, 1.0, 0.0, 0.0, 0.0]


def swept_region(
    session, component, dimension, values, heights_mm, axis="z", margin_mm=0.0, tolerance_mm=0.2, frame=None
):
    from .assembly import _transform_data, sample_dimension, transform_point

    _axis(axis)
    heights = _heights(heights_mm)
    margin, tolerance = number(margin_mm, "margin_mm"), number(tolerance_mm, "tolerance_mm")
    if margin < 0 or tolerance <= 0:
        raise CadError("INVALID_ARGUMENT", "Use nonnegative margin and positive sampling tolerance.")
    target = find_component(session, component)
    base = None
    if frame not in (None, "world", "assembly", "model"):
        reference = target if frame == "object" else find_component(session, frame)
        base = _inverse_frame(_transform_data(reference))
    outlines = [[] for _ in heights]

    def observe(value):
        triangles = native_triangles(session, component)
        if base is not None:
            triangles = [
                tuple(tuple(v * 1000 for v in transform_point(base, [x / 1000 for x in p])) for p in t)
                for t in triangles
            ]
        cross_sections = sections(triangles, axis, heights, min(tolerance, 0.0001))
        for points, section in zip(outlines, cross_sections):
            points.extend(p for edge in section["segments_mm"] for p in edge)
        return {"section_count": len(cross_sections), "triangle_count": len(triangles)}

    result = sample_dimension(session, dimension, values, observe)
    result.update(
        component=component,
        axis=axis,
        frame="assembly" if frame is None else frame,
        frame_transform=base,
        requested_tolerance_mm=tolerance,
        margin_mm=margin,
        method="convex_hull_of_explicit_sampled_sections",
        continuous_sweep_verified=False,
        native_tessellation_tolerance_controlled=False,
        sections=[
            {"height_mm": h, "outline_mm": [list(p) for p in offset_hull(convex_hull(points), margin)]}
            for h, points in zip(heights, outlines)
        ],
        limitations=[
            "A sampled convex hull fills concavities and gaps between disconnected regions.",
            "Intermediate joint positions are not inferred or certified; requested tolerance does not control native graphics faceting.",
        ],
    )
    return result


TOOLS = {"slice_mesh": slice_mesh, "compare_with_mesh": compare_with_mesh, "swept_region": swept_region}
