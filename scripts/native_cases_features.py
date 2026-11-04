"""Fresh-part native acceptance recipes for all 24 independent feature tools.

The runner owns the application and creates a separate part for every case.
These recipes only contain data and elementary, independently derived volume
checks. Importing this module never connects to CAD. The modeled-thread case
requires the installed SOLIDWORKS Metric Die profile; missing assets must be
reported as a failed case, never as a skipped or simulated success.
"""
from math import pi


def _plate(width=80, height=50, depth=10):
    return [{"tool": "add_box", "args": {"width_mm": width, "height_mm": height,
        "depth_mm": depth, "name": "FixturePlate"}, "bind": "base"}]


def _hole(name, x=20, y=25, diameter=6):
    return {"tool": "add_hole", "args": {"diameter_mm": diameter, "x_mm": x,
        "y_mm": y, "name": name}}


def _plane(name, distance):
    return {"tool": "add_plane", "args": {"base": "front", "offset_mm": distance, "name": name}}


def _case(tool, args, *, setup=None, volume=None, bodies=None, result=None, paths=None):
    case = {"id": tool, "setup": [] if setup is None else setup, "tool": tool, "args": args}
    check = {}
    if volume is not None:
        check["volume_mm3"] = volume
    if bodies is not None:
        check["body_count"] = bodies
    if result is not None:
        check["result"] = result
    if paths is not None:
        check["path_equals"] = paths
    if check:
        case["check"] = check
    return case


def _two_overlapping_bodies():
    return [
        {"tool": "add_extruded_profile", "args": {"points_mm": [
            [0, 0], [20, 0], [20, 20], [0, 20]], "depth_mm": 10,
            "merge": False, "name": "MainSolid"}},
        {"tool": "add_extruded_profile", "args": {"points_mm": [
            [10, 0], [30, 0], [30, 20], [10, 20]], "depth_mm": 10,
            "merge": False, "name": "ToolSolid"}},
        {"tool": "list_bodies", "args": {}, "bind": "solids"},
    ]


CASES = [
    _case("list_bodies", {}, setup=_plate(), volume=40000, bodies=1,
        result={"count": 1}, paths={"bodies.0.volume_mm3": 40000}),

    # 20x20x10 plus 20x20x10, with a 10x20x10 overlap, becomes one 30x20x10 body.
    _case("combine_bodies", {"operation": "add", "main": "$solids.bodies.0.id",
        "tools": ["$solids.bodies.1.id"], "name": "Union"},
        setup=_two_overlapping_bodies(), volume=6000, bodies=1,
        result={"operation": "add", "tool_count": 1}),

    _case("split_body", {"plane": "MidThickness", "name": "BisectedPlate"},
        setup=_plate()+[_plane("MidThickness", 5)], volume=40000, bodies=2,
        result={"body_count": 2, "external_files_created": False}),

    _case("add_thread", {"size": "M10x1.5", "x_mm": 0, "y_mm": 0, "z_mm": 20,
        "length_mm": 10, "internal": False, "name": "ModeledThread"},
        setup=[{"tool": "add_disc", "args": {"diameter_mm": 10, "thickness_mm": 20,
            "name": "ThreadBlank"}}], bodies=1,
        result={"feature": "ModeledThread", "size": "M10x1.5", "pitch_mm": 1.5,
            "nominal_diameter_mm": 10., "length_mm": 10., "internal": False}),

    # Four vertical corners replace four r*r squares by four quarter circles.
    _case("add_fillet", {"radius_mm": 2, "edges": "z", "name": "VerticalFillets"},
        setup=_plate(), volume=40000-(4-pi)*2**2*10, bodies=1,
        result={"edge_count": 4, "radius_mm": 2}),

    # The 20 mm wide top becomes an R10 semicircle above a 20 mm tall rectangle.
    _case("add_full_round", {"face": "+z", "x_mm": 40, "y_mm": 10, "z_mm": 30,
        "name": "RoundedCrown"}, setup=_plate(80, 20, 30),
        volume=80*(20*20+pi*10**2/2), bodies=1,
        result={"feature": "RoundedCrown"}),

    # Four 45-degree chamfers remove four triangular 2x2/2 prisms.
    _case("add_chamfer", {"distance_mm": 2, "edges": "z", "angle_deg": 45,
        "name": "VerticalChamfers"}, setup=_plate(), volume=40000-4*(2*2/2)*10,
        bodies=1, result={"edge_count": 4, "angle_deg": 45.}),

    _case("add_linear_pattern", {"count": 3, "spacing_mm": 20, "direction": "+x",
        "feature_name": "LinearSeed"}, setup=_plate()+[_hole("LinearSeed", x=10)],
        volume=40000-3*pi*3**2*10, bodies=1,
        result={"instances": 3, "seed": "LinearSeed", "direction": "+x"}),

    _case("add_circular_pattern", {"count": 4, "center_x_mm": 40, "center_y_mm": 25,
        "feature_name": "CircularSeed"}, setup=_plate()+[_hole("CircularSeed", x=55)],
        volume=40000-4*pi*3**2*10, bodies=1,
        result={"instances": 4, "seed": "CircularSeed", "axis": "z"}),

    _case("add_mirror", {"plane": "right", "offset_mm": 40,
        "features": ["MirrorSeed"], "name": "SymmetricHoles"},
        setup=_plate()+[_hole("MirrorSeed")], volume=40000-2*pi*3**2*10,
        bodies=1, result={"feature": "SymmetricHoles", "seeds": ["MirrorSeed"]}),

    _case("add_shell", {"thickness_mm": 2, "open_face": "+z"}, setup=_plate(),
        volume=80*50*10-76*46*8, bodies=1,
        result={"thickness_mm": 2, "opened_faces": 1}),

    _case("set_dimension", {"dimension_name": "$base.depth_dimension", "value_mm": 15},
        setup=_plate(), volume=60000, bodies=1,
        result={"previous_value": 10., "value": 15., "unit": "mm", "rebuild_ok": True}),

    _case("list_dimensions", {"contains": "$base.depth_dimension"}, setup=_plate(),
        volume=40000, result={"count": 1},
        paths={"dimensions.0.value": 10., "dimensions.0.unit": "mm", "dimensions.0.driving": True}),

    _case("read_sketch", {"name": "InspectionOutline"}, setup=_plate()+[
        {"tool": "add_sketch", "args": {"plane": "front", "start_mm": [5, 5],
            "segments": [{"line": [25, 5]}, {"line": [25, 15]},
                         {"line": [5, 15]}, {"line": [5, 5]}], "name": "InspectionOutline"}}],
        volume=40000, result={"count": 4, "coordinate_system": "model", "unit": "mm"},
        paths={"segments.0.type": "line", "segments.0.length_mm": 20.,
            "segments.1.length_mm": 10., "segments.0.start_mm": [5., 5., 0.],
            "segments.0.end_mm": [25., 5., 0.]}),

    # Native templates add varying system folders; their count is deliberately not guessed.
    _case("list_features", {}, setup=_plate()+[_plane("InspectionPlane", 20)], volume=40000),

    _case("delete_feature", {"name": "TemporaryHole", "with_children": True},
        setup=_plate()+[_hole("TemporaryHole")], volume=40000, bodies=1,
        result={"feature": "TemporaryHole", "deleted": True, "rebuild_ok": True, "broken_features": []}),

    _case("suppress_feature", {"name": "SuppressedHole", "suppress": True},
        setup=_plate()+[_hole("SuppressedHole")], volume=40000, bodies=1,
        result={"name": "SuppressedHole", "suppressed": True, "previous_suppressed": False,
            "rebuild_ok": True, "broken_features": []}),

    _case("reorder_feature", {"name": "LaterPlane", "before": "EarlierPlane"},
        setup=_plate()+[_plane("EarlierPlane", 20), _plane("LaterPlane", 30)],
        volume=40000, result={"name": "LaterPlane", "before": "EarlierPlane", "verified": True,
            "rebuild_ok": True, "broken_features": []}),

    _case("set_appearance", {"rgb": [40, 120, 200], "transparency": .2}, setup=_plate(),
        volume=40000, result={"changed": True, "rgb": [40, 120, 200], "transparency": .2, "saved": False}),

    _case("set_material", {"name": "Native Acceptance Steel", "density_kg_m3": 7850},
        setup=_plate(), volume=40000,
        result={"material": "Native Acceptance Steel", "density_only": True, "saved": False},
        paths={"mass_properties.density_kg_m3": 7850., "mass_properties.mass_kg": .314}),

    # D1 is the native extrusion-depth parameter of the explicitly named fixture feature.
    _case("set_equation", {"equation": '"D1@FixturePlate" = 15mm'}, setup=_plate(),
        volume=60000, result={"count": 1, "changed": ["D1@FixturePlate"], "rebuild_ok": True},
        paths={"equations.0.valid": True}),

    _case("set_equations", {"equations": ['"NativeDepth" = 15mm',
        '"D1@FixturePlate" = "NativeDepth"']}, setup=_plate(), volume=60000,
        result={"count": 2, "changed": ["NativeDepth", "D1@FixturePlate"], "rebuild_ok": True},
        paths={"equations.0.valid": True, "equations.1.valid": True}),

    _case("list_equations", {}, setup=_plate()+[
        {"tool": "set_equation", "args": {"equation": '"NativeDepth" = 15mm'}}],
        volume=40000, result={"count": 1}, paths={"equations.0.name": "NativeDepth",
            "equations.0.global": True, "equations.0.valid": True}),

    _case("delete_equation", {"equation": "NativeDepth"}, setup=_plate()+[
        {"tool": "set_equation", "args": {"equation": '"NativeDepth" = 15mm'}}],
        volume=40000, result={"index": 0, "rebuild_ok": True, "broken_features": []}),
]
