"""Bounded STL inspection and offline views. STL coordinates are interpreted as mm.

STL carries no unit metadata. Topology is checked with exact vertex coordinates;
we never silently weld, repair, or claim that an open mesh has a solid volume.
"""
from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Iterable

MAX_BYTES = 64 * 1024 * 1024
MAX_TRIANGLES = 200_000
MAX_TOPOLOGY_EDGES = 600_000
MAX_VIEWER_TRIANGLES = 50_000
MAX_SVG_TRIANGLES = 20_000
Point = tuple[float, float, float]
Triangle = tuple[Point, Point, Point]


class MeshError(ValueError):
    """The input is invalid or exceeds a documented resource limit."""


def _point(values: Iterable[object]) -> Point:
    try:
        xyz = tuple(float(v) for v in values)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MeshError("STL coordinate is not a number") from exc
    if len(xyz) != 3 or not all(math.isfinite(v) and abs(v) <= 1e12 for v in xyz):
        raise MeshError("STL coordinates must be finite and within +/-1e12 mm")
    return xyz  # type: ignore[return-value]


def _read_stl(path: Path) -> list[Triangle]:
    path = Path(path)
    if not path.is_file():
        raise MeshError(f"STL file does not exist: {path}")
    if path.stat().st_size > MAX_BYTES:
        raise MeshError(f"STL exceeds the {MAX_BYTES}-byte limit")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise MeshError(f"STL exceeds the {MAX_BYTES}-byte limit")
    # Binary headers may start with 'solid'; exact record length disambiguates.
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + count * 50:
            if not 0 < count <= MAX_TRIANGLES:
                raise MeshError(f"STL must have 1..{MAX_TRIANGLES} triangles")
            triangles = []
            for record in struct.iter_unpack("<12fH", data[84:]):
                _point(record[:3])  # Reject non-finite supplied normals too.
                triangles.append((_point(record[3:6]), _point(record[6:9]), _point(record[9:12])))
            return triangles
    try:
        source = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MeshError("Invalid binary STL length or non-ASCII text STL") from exc
    triangles: list[Triangle] = []
    vertices: list[Point] = []
    state = "solid"
    for number, line in enumerate(source.splitlines(), 1):
        words = line.strip().split()
        if not words:
            continue
        keyword = words[0].lower()
        valid = False
        if state == "solid" and keyword == "solid":
            state, valid = "facet", True
        elif state == "facet" and keyword == "facet" and len(words) == 5 and words[1].lower() == "normal":
            _point(words[2:])
            vertices, state, valid = [], "outer", True
        elif state == "outer" and [w.lower() for w in words] == ["outer", "loop"]:
            state, valid = "vertex", True
        elif state == "vertex" and keyword == "vertex" and len(words) == 4:
            vertices.append(_point(words[1:]))
            if len(vertices) == 3:
                state = "endloop"
            valid = True
        elif state == "endloop" and [w.lower() for w in words] == ["endloop"]:
            state, valid = "endfacet", True
        elif state == "endfacet" and [w.lower() for w in words] == ["endfacet"]:
            triangles.append(tuple(vertices))  # type: ignore[arg-type]
            if len(triangles) > MAX_TRIANGLES:
                raise MeshError(f"STL exceeds the {MAX_TRIANGLES}-triangle limit")
            state, valid = "facet", True
        elif state == "facet" and keyword == "endsolid":
            state, valid = "solid", True
        if not valid:
            raise MeshError(f"Malformed ASCII STL at line {number}: expected {state}")
    if state != "solid" or not triangles:
        raise MeshError("ASCII STL is empty, truncated, or missing endsolid")
    return triangles


def _cross(a: Point, b: Point) -> Point:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub(a: Point, b: Point) -> Point:
    return tuple(x - y for x, y in zip(a, b))  # type: ignore[return-value]


def _summary(triangles: list[Triangle]) -> dict:
    low = [min(v[i] for t in triangles for v in t) for i in range(3)]
    high = [max(v[i] for t in triangles for v in t) for i in range(3)]
    edges: dict[tuple[Point, Point], list[int]] = {}
    degenerates = 0
    surface_terms = []
    volume_terms = []
    origin = triangles[0][0]
    for a, b, c in triangles:
        cross = _cross(_sub(b, a), _sub(c, a))
        area2 = math.hypot(*cross)
        degenerates += int(area2 == 0)
        surface_terms.append(area2 / 2)
        local_a, local_b, local_c = _sub(a, origin), _sub(b, origin), _sub(c, origin)
        bc = _cross(local_b, local_c)
        volume_terms.append(sum(x * y for x, y in zip(local_a, bc)) / 6)
        for u, v in ((a, b), (b, c), (c, a)):
            edge = (u, v) if u < v else (v, u)
            record = edges.setdefault(edge, [0, 0])
            record[0] += 1
            record[1] += 1 if u < v else -1
        if len(edges) > MAX_TOPOLOGY_EDGES:
            raise MeshError("Mesh exceeds the topology edge limit")
    boundary = sum(count == 1 for count, _ in edges.values())
    nonmanifold = sum(count > 2 for count, _ in edges.values())
    inconsistent = sum(count == 2 and balance != 0 for count, balance in edges.values())
    closed = boundary == 0 and nonmanifold == 0 and degenerates == 0
    oriented = closed and inconsistent == 0
    result = {
        "units": "mm",
        "units_note": "STL has no units; coordinates are interpreted as millimetres.",
        "triangle_count": len(triangles),
        "bounds": {"min": low, "max": high, "size": [hi - lo for lo, hi in zip(low, high)]},
        "surface_area_mm2": math.fsum(surface_terms),
        "topology": {
            "method": "exact coordinate edge matching; no automatic welding",
            "edge_count": len(edges),
            "boundary_edges": boundary,
            "nonmanifold_edges": nonmanifold,
            "inconsistent_orientation_edges": inconsistent,
            "degenerate_triangles": degenerates,
            "watertight": closed,
            "consistently_oriented": oriented,
            "self_intersections_checked": False,
        },
    }
    if oriented:
        result["volume_mm3"] = abs(math.fsum(volume_terms))
        result["volume_note"] = "Signed mesh volume magnitude; assumes no self-intersections and correctly oriented disconnected shells."
    return result


def inspect_stl(path: Path) -> dict:
    """Inspect an ASCII/binary STL; return volume only for a closed oriented mesh."""
    return _summary(_read_stl(Path(path)))


def _write_new(path: Path, text: str) -> Path:
    """Publish a complete UTF-8 file atomically, without overwriting an existing path."""
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".swpro-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link creation is atomic and fails if the destination appeared in
        # the meantime. Unlike replace(), it cannot overwrite another writer.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _script_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build_viewer(stl_path: Path, output_path: Path, *, title: str = "SolidWorks MCP PRO") -> dict:
    """Create a local standalone orbit/zoom viewer with no network dependencies."""
    triangles = _read_stl(Path(stl_path))
    summary = _summary(triangles)
    stride = max(1, math.ceil(len(triangles) / MAX_VIEWER_TRIANGLES))
    shown = triangles[::stride]
    payload = {"title": str(title), "summary": summary, "triangles": shown, "displayed_triangles": len(shown), "sampled": stride > 1}
    template = Path(__file__).with_name("viewer.html").read_text(encoding="utf-8")
    replacements = {"__SWPRO_TITLE__": html.escape(str(title)), "__SWPRO_DATA__": _script_json(payload)}
    output = re.sub(r"__SWPRO_(?:TITLE|DATA)__", lambda match: replacements[match.group()], template)
    path = _write_new(Path(output_path), output)
    return {"viewer_path": str(path), "mime_type": "text/html", "displayed_triangles": len(shown), "sampled": stride > 1, "mesh": summary}


def render_svg(stl_path: Path, output_path: Path, *, title: str = "SolidWorks MCP PRO") -> dict:
    """Create a bounded static orthographic SVG. Large meshes are visibly sampled."""
    triangles = _read_stl(Path(stl_path))
    summary = _summary(triangles)
    stride = max(1, math.ceil(len(triangles) / MAX_SVG_TRIANGLES))
    center = [(a + b) / 2 for a, b in zip(summary["bounds"]["min"], summary["bounds"]["max"])]
    def project(v: Point) -> tuple[float, float, float]:
        x, y, z = [a - b for a, b in zip(v, center)]
        return (0.70710678 * (x - z), -0.81649658 * y + 0.40824829 * (x + z), 0.57735027 * (x + y + z))
    projected = [tuple(project(v) for v in t) for t in triangles[::stride]]
    span = max(max(abs(v[0]), abs(v[1])) for t in projected for v in t) or 1
    scale = 290 / span
    shapes = []
    for tri in sorted(projected, key=lambda t: sum(v[2] for v in t)):
        points = " ".join(f"{400 + v[0] * scale:.3f},{370 + v[1] * scale:.3f}" for v in tri)
        shapes.append(f'<polygon points="{points}" fill="#8bbed4" stroke="#28495a" stroke-width="0.4"/>')
    label = html.escape(str(title))
    sampled = " (sampled preview)" if stride > 1 else ""
    output = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 720" role="img" aria-label="{label}"><title>{label}</title><rect width="800" height="720" fill="#101b24"/><text x="32" y="45" fill="#eaf3f7" font-family="sans-serif" font-size="24">{label}</text>{"".join(shapes)}<text x="32" y="695" fill="#eaf3f7" font-family="sans-serif" font-size="14">{len(triangles):,} triangles · mm{sampled}</text></svg>'
    path = _write_new(Path(output_path), output)
    return {"image_path": str(path), "mime_type": "image/svg+xml", "displayed_triangles": len(projected), "sampled": stride > 1, "mesh": summary}
