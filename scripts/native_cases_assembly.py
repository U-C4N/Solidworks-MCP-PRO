"""Owned fixtures for 20 independently implemented inspection and assembly tools.

The runner creates a fresh part and an exclusive output directory for each case.
All native documents and mesh exports below belong to that run. Motion tools
must verify dimension and placement restoration before fixture cleanup. Importing
this module performs no COM calls and does not inspect an existing user model.
"""


def _box(width=40, *, name="FixtureBox"):
    return {
        "tool": "add_box",
        "args": {"width_mm": width, "height_mm": 20, "depth_mm": 10, "name": name},
        "bind": "box",
    }


def _part():
    return [_box()]


def _assembly(second_x=60, *, second=True):
    steps = [
        _box(),
        {"tool": "save_part", "args": {"path": "$output/source.sldprt"}},
        {"tool": "new_assembly", "args": {}},
        {"tool": "insert_component", "args": {"path": "$output/source.sldprt", "fixed": True}, "bind": "a"},
    ]
    if second:
        steps.append(
            {
                "tool": "insert_component",
                "args": {"path": "$output/source.sldprt", "x_mm": second_x, "fixed": False},
                "bind": "b",
            }
        )
    return steps


def _joint_args():
    return {
        "comp_a": "$a.component.name",
        "face_a": "+x",
        "comp_b": "$b.component.name",
        "face_b": "-x",
        "mate_type": "distance",
        "distance_mm": 20,
    }


def _joint():
    return _assembly() + [{"tool": "add_mate", "args": _joint_args(), "bind": "joint"}]


def _mesh():
    return _part() + [{"tool": "export", "args": {"path": "$output/reference.stl", "file_format": "stl"}}]


def _case(tool, args, setup, volume):
    return {"id": tool, "setup": setup, "tool": tool, "args": args, "check": {"volume_mm3": volume}}


CASES = [
    _case("get_mass_properties", {}, _part(), 8000),
    _case("get_bounding_box", {}, _part(), 8000),
    _case("list_faces", {}, _part(), 8000),
    _case("list_edges", {"face": "+z", "min_length_mm": 15}, _part(), 8000),
    _case("check_printability", {"up": "+z", "overhang_deg": 45, "min_wall_mm": 2}, _part(), 8000),
    _case(
        "insert_component",
        {"path": "$output/source.sldprt", "x_mm": 60, "fixed": False},
        _assembly(second=False),
        16000,
    ),
    _case("list_components", {}, _assembly(), 16000),
    _case("delete_component", {"component": "$b.component.name"}, _assembly(), 8000),
    _case(
        "replace_component",
        {"name": "$a.component.name", "path": "$output/replacement.sldprt"},
        [
            _box(),
            {"tool": "save_part", "args": {"path": "$output/source.sldprt"}},
            {"tool": "new_part", "args": {}},
            _box(20, name="ReplacementBox"),
            {"tool": "save_part", "args": {"path": "$output/replacement.sldprt"}},
            {"tool": "new_assembly", "args": {}},
            {
                "tool": "insert_component",
                "args": {"path": "$output/source.sldprt", "fixed": True},
                "bind": "a",
            },
            {
                "tool": "insert_component",
                "args": {"path": "$output/source.sldprt", "x_mm": 60, "fixed": False},
                "bind": "b",
            },
        ],
        12000,
    ),
    _case(
        "set_component_transform",
        {"name": "$b.component.name", "x_mm": 80, "y_mm": 10, "z_mm": 0, "rz_deg": 45},
        _assembly(),
        16000,
    ),
    _case("add_mate", _joint_args(), _assembly(), 16000),
    _case("suppress_mate", {"name": "$joint.mate", "suppress": True}, _joint(), 16000),
    _case("delete_mate", {"name": "$joint.mate"}, _joint(), 16000),
    _case("check_interference", {}, _assembly(second_x=20), 16000),
    _case(
        "measure_distance",
        {"component_a": "$a.component.name", "component_b": "$b.component.name"},
        _assembly(),
        16000,
    ),
    _case("get_assembly_bounding_box", {}, _assembly(), 16000),
    _case(
        "check_motion",
        {
            "dimension_name": "$joint.dimension_name",
            "values": [10, 20, 30],
            "distances": [["$a.component.name", "$b.component.name"]],
        },
        _joint(),
        16000,
    ),
    _case(
        "slice_mesh", {"path": "$output/reference.stl", "axis": "z", "heights_mm": [0, 5, 10]}, _mesh(), 8000
    ),
    _case(
        "compare_with_mesh",
        {"path": "$output/reference.stl", "axis": "z", "heights_mm": [2, 5, 8], "offset_mm": [0, 0, 0]},
        _mesh(),
        8000,
    ),
    _case(
        "swept_region",
        {
            "component": "$b.component.name",
            "dimension": "$joint.dimension_name",
            "values": [10, 20, 30],
            "heights_mm": [5],
            "axis": "z",
            "margin_mm": 1,
            "tolerance_mm": 0.2,
            "frame": "assembly",
        },
        _joint(),
        16000,
    ),
]


_EXPECTED = {
    "get_mass_properties": {
        "result": {
            "mass_properties": {
                "volume_mm3": 8000,
                "center_of_mass_mm": [20, 10, 5],
                "surface_area_mm2": 2800,
            }
        }
    },
    "get_bounding_box": {
        "result": {
            "bounding_box": {"min_mm": [0, 0, 0], "max_mm": [40, 20, 10], "size_mm": [40, 20, 10]},
            "approximate": False,
        }
    },
    "list_faces": {"result": {"count": 6}},
    "list_edges": {"result": {"count": 4}},
    "check_printability": {
        "result": {
            "passed": None,
            "status": "unknown",
            "overhang_area_mm2": 0,
            "wall_check": {"complete": False, "violations": []},
        }
    },
    "insert_component": {
        "result": {
            "inserted": True,
            "verified": True,
            "component": {"position_mm": [60, 0, 0], "fixed": False},
        }
    },
    "list_components": {"result": {"count": 2}},
    "delete_component": {"result": {"verified": True, "deleted": "$b.component.name"}},
    "replace_component": {
        "result": {"verified": True},
        "path_equals": {"replacements.0.position_mm": [0, 0, 0]},
    },
    "set_component_transform": {
        "result": {
            "verified": True,
            "mates_solved": True,
            "component": {"position_mm": [80, 10, 0], "fixed": False},
        }
    },
    "add_mate": {"result": {"type": "distance", "verified": True, "native_error": 1}},
    "suppress_mate": {"result": {"suppressed": True, "verified": True, "mate": "$joint.mate"}},
    "delete_mate": {"result": {"verified": True, "deleted": "$joint.mate"}},
    "check_interference": {
        "result": {"count": 1, "clear": False, "cleanup": {"verified": True}},
        "path_equals": {"interferences.0.volume_mm3": 4000},
    },
    "measure_distance": {"result": {"distance_mm": 20, "temporary_geometry_inserted": False}},
    "get_assembly_bounding_box": {
        "result": {
            "bounding_box": {"min_mm": [0, 0, 0], "max_mm": [100, 20, 10], "size_mm": [100, 20, 10]},
            "approximate": False,
        }
    },
    "check_motion": {
        "result": {"passed": True, "restoration": {"verified": True}, "continuous_motion_verified": False},
        "path_equals": {
            "samples.0.distances.0.distance_mm": 10,
            "samples.1.distances.0.distance_mm": 20,
            "samples.2.distances.0.distance_mm": 30,
        },
    },
    "slice_mesh": {
        "path_equals": {"sections.0.area_mm2": 800, "sections.1.area_mm2": 800, "sections.2.area_mm2": 800}
    },
    "compare_with_mesh": {
        "result": {"exact_geometry_equivalence_verified": False},
        "path_equals": {
            "comparisons.0.cad.area_mm2": 800,
            "comparisons.1.cad.area_mm2": 800,
            "comparisons.2.cad.area_mm2": 800,
            "comparisons.0.mesh.area_mm2": 800,
            "comparisons.1.mesh.area_mm2": 800,
            "comparisons.2.mesh.area_mm2": 800,
        },
        # Native graphics coordinates are float32; preserve the measured
        # discrepancies and bound them explicitly in their respective units.
        "path_close": {
            "comparisons.0.area_difference_mm2": {"expected": 0, "abs_tolerance": 0.001},
            "comparisons.1.area_difference_mm2": {"expected": 0, "abs_tolerance": 0.001},
            "comparisons.2.area_difference_mm2": {"expected": 0, "abs_tolerance": 0.001},
            "comparisons.0.sampled_boundary_distance_mm": {"expected": 0, "abs_tolerance": 0.00001},
            "comparisons.1.sampled_boundary_distance_mm": {"expected": 0, "abs_tolerance": 0.00001},
            "comparisons.2.sampled_boundary_distance_mm": {"expected": 0, "abs_tolerance": 0.00001},
        },
    },
    "swept_region": {
        "result": {
            "restoration": {"verified": True},
            "continuous_sweep_verified": False,
            "native_tessellation_tolerance_controlled": False,
        },
        "path_equals": {"sections.0.outline_mm": [[49, -1], [111, -1], [111, 21], [49, 21]]},
    },
}

for _fixture in CASES:
    _fixture["check"].update(_EXPECTED[_fixture["id"]])
