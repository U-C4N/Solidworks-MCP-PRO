import struct
from types import SimpleNamespace as NS
import zipfile

import pytest

from solidworks_mcp_pro.native import mesh as api
from solidworks_mcp_pro.native.errors import CadError
from test_native_assembly import PartModel, Session, Component


VERTICES = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
FACES = [
    (0, 2, 1),
    (0, 3, 2),
    (4, 5, 6),
    (4, 6, 7),
    (0, 1, 5),
    (0, 5, 4),
    (1, 2, 6),
    (1, 6, 5),
    (2, 3, 7),
    (2, 7, 6),
    (3, 0, 4),
    (3, 4, 7),
]


def cube():
    return [tuple(VERTICES[i] for i in face) for face in FACES]


def write_stl(path, triangles=None):
    triangles = cube() if triangles is None else triangles
    data = b"Independent mesh fixture".ljust(80, b" ") + struct.pack("<I", len(triangles))
    for triangle in triangles:
        data += struct.pack("<12fH", 0, 0, 0, *[value for p in triangle for value in p], 0)
    path.write_bytes(data)
    return path


def write_3mf(
    path, unit="millimeter", extra="", build="1", transform="1 0 0 0 1 0 0 0 1 0 0 0", triangles=None
):
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in VERTICES)
    faces = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in (FACES if triangles is None else triangles)
    )
    xml = f'<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="{unit}"><resources><object id="1" type="model"><mesh><vertices>{vertices}</vertices><triangles>{faces}</triangles></mesh></object>{extra}</resources><build><item objectid="{build}" transform="{transform}"/></build></model>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", xml)
    return path


@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_triangle_slices_close_loops_and_measure_cube_area(axis):
    rows = api.sections(cube(), axis, [0, 0.5, 1, 2])
    assert all(r["closed"] for r in rows)
    assert [r["area_mm2"] for r in rows] == pytest.approx([1, 1, 1, 0])
    assert [r["perimeter_mm"] for r in rows] == pytest.approx([4, 4, 4, 0])


def test_coplanar_internal_triangle_diagonal_is_removed():
    triangles = [((0, 0, 0), (1, 0, 0), (1, 1, 0)), ((0, 0, 0), (1, 1, 0), (0, 1, 0))]
    section = api.sections(triangles, "z", [0])[0]
    assert len(section["segments_mm"]) == 4 and section["area_mm2"] == 1


def test_open_triangle_section_does_not_invent_closed_area():
    section = api.sections([((0, 0, 0), (1, 0, 1), (0, 1, 1))], "z", [0.5])[0]
    assert not section["closed"] and section["area_mm2"] is None


def test_nested_contour_area_subtracts_holes():
    # Independent wall meshes leave an inner square hole at the section.
    outer = cube()
    inner = [tuple((p[0] * 0.5 + 0.25, p[1] * 0.5 + 0.25, p[2]) for p in t) for t in cube()]
    section = api.sections(outer + inner, "z", [0.5])[0]
    assert section["closed"] and len(section["loops_mm"]) == 2
    assert section["area_mm2"] == pytest.approx(0.75)


def test_stl_slice_uses_authored_mm_coordinates_and_accepts_empty_heights(tmp_path):
    path = write_stl(tmp_path / "cube.stl")
    result = api.slice_mesh(None, str(path), "z", [0.5])
    assert result["triangle_count"] == 12 and result["sections"][0]["area_mm2"] == 1
    assert api.slice_mesh(None, str(path), "z", [])["sections"] == []


@pytest.mark.parametrize(
    "unit,factor", [("millimeter", 1), ("inch", 25.4), ("meter", 1000), ("micron", 0.001)]
)
def test_3mf_units_are_converted_before_mm_measurement(tmp_path, unit, factor):
    triangles = api.read_3mf(write_3mf(tmp_path / "cube.3mf", unit))
    assert max(p[0] for t in triangles for p in t) == factor
    area = api.sections(triangles, "z", [factor / 2])[0]["area_mm2"]
    assert area == pytest.approx(factor**2, rel=1e-4)


def test_3mf_nested_component_and_build_transforms_compose_in_model_units(tmp_path):
    extra = '<object id="2" type="model"><components><component objectid="1" transform="1 0 0 0 1 0 0 0 1 2 0 0"/></components></object>'
    path = write_3mf(
        tmp_path / "placed.3mf", "centimeter", extra, build="2", transform="1 0 0 0 1 0 0 0 1 3 0 0"
    )
    triangles = api.read_3mf(path)
    assert min(p[0] for t in triangles for p in t) == 50
    assert max(p[0] for t in triangles for p in t) == 60


@pytest.mark.parametrize("triangles", [[(-1, 1, 2)], [(0, 1, 9)]])
def test_3mf_rejects_invalid_vertex_indices(tmp_path, triangles):
    with pytest.raises(CadError, match="INVALID_3MF"):
        api.read_3mf(write_3mf(tmp_path / "bad.3mf", triangles=triangles))


def test_3mf_cycle_is_rejected(tmp_path):
    extra = '<object id="2"><components><component objectid="2"/></components></object>'
    with pytest.raises(CadError, match="cycle"):
        api.read_3mf(write_3mf(tmp_path / "cycle.3mf", extra=extra, build="2"))


def test_3mf_unsupported_unit_does_not_silently_become_mm(tmp_path):
    with pytest.raises(CadError, match="3MF_UNIT_UNSUPPORTED"):
        api.read_3mf(write_3mf(tmp_path / "units.3mf", "mile"))


def test_3mf_archive_paths_and_xml_entities_are_rejected(tmp_path):
    path = write_3mf(tmp_path / "unsafe.3mf")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("../outside", "bad")
    with pytest.raises(CadError, match="INVALID_3MF_ARCHIVE"):
        api.read_3mf(path)
    with zipfile.ZipFile(tmp_path / "entity.3mf", "w") as archive:
        archive.writestr("3D/3dmodel.model", '<!DOCTYPE model [<!ENTITY value "x">]><model/>')
    with pytest.raises(CadError, match="INVALID_3MF_XML"):
        api.read_3mf(tmp_path / "entity.3mf")


@pytest.mark.parametrize(
    "arguments", [("w", [0]), ("z", [float("nan")]), ("z", [True]), ("z", list(range(257)))]
)
def test_section_arguments_are_bounded_and_finite(arguments):
    with pytest.raises(CadError):
        api.sections(cube(), *arguments)


def test_comparison_uses_explicit_offset_and_labels_sampled_metric(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "native_triangles", lambda session: cube())
    result = api.compare_with_mesh(
        object(), str(write_stl(tmp_path / "cube.stl")), "z", [0.5], offset_mm=[1, 0, 0]
    )
    row = result["comparisons"][0]
    assert row["area_difference_mm2"] == 0 and row["sampled_boundary_distance_mm"] == 1
    assert result["exact_geometry_equivalence_verified"] is False


def test_native_part_tessellation_is_read_in_metres_then_returned_mm():
    model = PartModel()
    model.GetTessTriangles = lambda si: [value / 1000 for t in cube() for p in t for value in p]
    result = api.native_triangles(Session(model))
    assert len(result) == 12 and max(p[0] for t in result for p in t) == 1


def test_native_assembly_tessellation_applies_component_transform():
    model = PartModel(kind=2)
    comp = Component(model, "A-1", x=0.01)
    model.components = [comp]
    face = NS(GetTessTriangles=lambda si: [value / 1000 for t in cube() for p in t for value in p])
    comp.child.bodies[0].faces = [face]
    triangles = api.native_triangles(Session(model), "A-1")
    assert min(p[0] for t in triangles for p in t) == 10
    assert max(p[0] for t in triangles for p in t) == 11


def test_convex_hull_and_margin_are_geometrically_correct():
    hull = api.convex_hull([(0, 0), (2, 0), (2, 2), (0, 2), (1, 1), (0, 0)])
    assert len(hull) == 4 and api._polygon_area(hull) == 4
    expanded = api.offset_hull(hull, 1)
    assert min(p[0] for p in expanded) == -1 and max(p[1] for p in expanded) == 3
    assert api._polygon_area(expanded) == 16


def test_inverse_rigid_frame_roundtrips_native_coordinates():
    from solidworks_mcp_pro.native.assembly import _placement, transform_point

    values = _placement(10, 20, 30, rx=10, ry=-20, rz=15)
    inverse = api._inverse_frame(values)
    point = [0.1, 0.2, 0.3]
    assert transform_point(inverse, transform_point(values, point)) == pytest.approx(point)


def test_swept_region_combines_explicit_joint_samples_and_restores_state(monkeypatch):
    model = PartModel(kind=2)
    model.components = [Component(model, "A-1", x=0.01)]
    session = Session(model)
    monkeypatch.setattr(
        api,
        "native_triangles",
        lambda session, component_name=None: [
            tuple((p[0] + model.dim.SystemValue * 1000, p[1], p[2]) for p in t) for t in cube()
        ],
    )
    result = api.swept_region(session, "A-1", "D1@Joint", [10, 20], [0.5], margin_mm=1)
    assert result["ok"] and result["restoration"]["verified"]
    hull = result["sections"][0]["outline_mm"]
    assert min(p[0] for p in hull) == 9 and max(p[0] for p in hull) == 22
    assert result["continuous_sweep_verified"] is False and model.dim.SystemValue == 0.01


def test_swept_region_can_use_fixed_initial_component_frame(monkeypatch):
    model = PartModel(kind=2)
    model.components = [Component(model, "A-1", x=0.01)]
    session = Session(model)
    monkeypatch.setattr(
        api,
        "native_triangles",
        lambda session, component_name=None: [
            tuple((p[0] + model.dim.SystemValue * 1000, p[1], p[2]) for p in t) for t in cube()
        ],
    )
    result = api.swept_region(session, "A-1", "D1@Joint", [10, 20], [0.5], frame="object")
    assert result["ok"] and min(p[0] for p in result["sections"][0]["outline_mm"]) == pytest.approx(0)


def test_native_tessellation_malformed_array_is_not_a_success():
    model = PartModel()
    model.GetTessTriangles = lambda si: [1, 2]
    with pytest.raises(CadError, match="TESSELLATION_UNAVAILABLE"):
        api.native_triangles(Session(model))
