from pathlib import Path
import json
import math
import struct
import pytest

from solidworks_mcp_pro import visualization as viz


TETRA = [
    ((0, 0, 0), (0, 1, 0), (1, 0, 0)),
    ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 0), (0, 0, 1), (0, 1, 0)),
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
]


def binary(path, triangles=TETRA, header=b"solid binary header"):
    path.write_bytes(header.ljust(80, b" ") + struct.pack("<I", len(triangles)) + b"".join(struct.pack("<12fH", 0, 0, 1, *(v for point in triangle for v in point), 0) for triangle in triangles))
    return path


def ascii_stl(path, triangles=TETRA):
    facets = []
    for triangle in triangles:
        facets.append("facet normal 0 0 1\nouter loop\n" + "\n".join("vertex " + " ".join(map(str, v)) for v in triangle) + "\nendloop\nendfacet")
    path.write_text("solid tetra\n" + "\n".join(facets) + "\nendsolid tetra", encoding="ascii")
    return path


@pytest.mark.parametrize("writer", [binary, ascii_stl])
def test_closed_oriented_volume_and_bounds(tmp_path, writer):
    summary = viz.inspect_stl(writer(tmp_path / "shape.stl"))
    assert summary["triangle_count"] == 4
    assert summary["bounds"] == {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]}
    assert summary["topology"]["watertight"] is True
    assert summary["topology"]["consistently_oriented"] is True
    assert summary["volume_mm3"] == pytest.approx(1 / 6)
    assert summary["surface_area_mm2"] == pytest.approx(1.5 + math.sqrt(3) / 2)


def test_open_and_reversed_face_do_not_report_volume(tmp_path):
    opened = viz.inspect_stl(binary(tmp_path / "open.stl", TETRA[:-1]))
    assert opened["topology"]["boundary_edges"] == 3
    assert "volume_mm3" not in opened
    reversed_face = [tuple(reversed(TETRA[0])), *TETRA[1:]]
    inconsistent = viz.inspect_stl(binary(tmp_path / "bad.stl", reversed_face))
    assert inconsistent["topology"]["inconsistent_orientation_edges"] == 3
    assert "volume_mm3" not in inconsistent


@pytest.mark.parametrize("content", [b"", b"solid missing end", b"solid s\nvertex 1 2 3\nendsolid", b"solid s\nfacet normal nan 0 1\nendsolid", b"X" * 84, b"\x00" * 83])
def test_rejects_malformed(tmp_path, content):
    path = tmp_path / "bad.stl"
    path.write_bytes(content)
    with pytest.raises(viz.MeshError):
        viz.inspect_stl(path)


def test_rejects_binary_nonfinite_and_truncated(tmp_path):
    path = binary(tmp_path / "bad.stl", [((math.inf, 0, 0), (0, 1, 0), (0, 0, 1))])
    with pytest.raises(viz.MeshError, match="finite"):
        viz.inspect_stl(path)
    path = binary(path)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(viz.MeshError):
        viz.inspect_stl(path)


def test_limits(tmp_path, monkeypatch):
    path = binary(tmp_path / "shape.stl")
    monkeypatch.setattr(viz, "MAX_TRIANGLES", 3)
    with pytest.raises(viz.MeshError, match="triangles"):
        viz.inspect_stl(path)
    monkeypatch.setattr(viz, "MAX_TRIANGLES", 10)
    monkeypatch.setattr(viz, "MAX_BYTES", 100)
    with pytest.raises(viz.MeshError, match="byte"):
        viz.inspect_stl(path)


def test_html_escapes_script_terminator_and_is_offline(tmp_path):
    path = binary(tmp_path / "shape.stl")
    target = tmp_path / "viewer.html"
    title = '</script><script>alert("x")</script>&'
    result = viz.build_viewer(path, target, title=title)
    content = target.read_text(encoding="utf-8")
    assert title not in content
    raw = content.split('<script id="mesh-data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(raw)["title"] == title
    assert "fetch(" not in content and 'src="http' not in content
    assert result["mesh"]["volume_mm3"] == pytest.approx(1 / 6)
    assert Path(result["viewer_path"]).is_absolute()
    with pytest.raises(FileExistsError):
        viz.build_viewer(path, target)
    assert target.read_text(encoding="utf-8") == content
    assert not list(tmp_path.glob(".swpro-*.tmp"))


def test_svg_safe_and_sampled_viewer(tmp_path, monkeypatch):
    path = binary(tmp_path / "shape.stl")
    monkeypatch.setattr(viz, "MAX_VIEWER_TRIANGLES", 2)
    result = viz.build_viewer(path, tmp_path / "view.html")
    assert result["sampled"] and result["displayed_triangles"] == 2
    assert result["mesh"]["triangle_count"] == 4
    svg = tmp_path / "view.svg"
    viz.render_svg(path, svg, title='<img onerror="x">')
    assert "<img" not in svg.read_text(encoding="utf-8")


def test_degenerate_mesh_has_no_volume(tmp_path):
    result = viz.inspect_stl(binary(tmp_path / "flat.stl", [((0, 0, 0), (0, 0, 0), (0, 0, 0))]))
    assert result["topology"]["degenerate_triangles"] == 1
    assert "volume_mm3" not in result


def test_large_translation_does_not_change_volume(tmp_path):
    moved = [tuple(tuple(c + 1e5 for c in v) for v in t) for t in TETRA]
    result = viz.inspect_stl(binary(tmp_path / "moved.stl", moved))
    assert result["volume_mm3"] == pytest.approx(1 / 6)


def test_template_markers_in_title_are_literal(tmp_path):
    path = binary(tmp_path / "shape.stl")
    target = tmp_path / "viewer.html"
    viz.build_viewer(path, target, title="__SWPRO_TITLE__ __SWPRO_DATA__")
    content = target.read_text(encoding="utf-8")
    raw = content.split('<script id="mesh-data" type="application/json">')[1].split("</script>")[0]
    assert json.loads(raw)["title"] == "__SWPRO_TITLE__ __SWPRO_DATA__"
