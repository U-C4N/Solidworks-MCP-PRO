"""Pre-COM contracts for the pinned native tool set, without coercing geometry.

Unknown tools/properties retain their upstream schema. These checks only cover
documented, state-independent input requirements; CAD still verifies topology.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path


FILE_WRITE_TOOLS = frozenset({"save_part", "save_assembly", "export", "screenshot", "make_drawing"})
MAX_ARGUMENT_BYTES = 2 * 1024 * 1024
NUMBER = {"type": "number"}
POSITIVE = {"type": "number", "exclusiveMinimum": 0}
NONNEGATIVE = {"type": "number", "minimum": 0}


def vector(size: int) -> dict:
    return {"type": "array", "items": NUMBER, "minItems": size, "maxItems": size}


def array(items: dict, minimum: int = 1, maximum: int | None = None) -> dict:
    result = {"type": "array", "items": items, "minItems": minimum}
    if maximum is not None:
        result["maxItems"] = maximum
    return result


POINT2, POINT3 = vector(2), vector(3)
POLYGON2 = array(POINT2, 3)
POLYGON3 = array(POINT3, 3)
POLYGON_PLANE = {"anyOf": [POLYGON2, POLYGON3]}
RADII = {"anyOf": [NONNEGATIVE, array(NONNEGATIVE)]}
SEGMENT = {
    "type": "object",
    "properties": {"line": POINT2, "arc": POINT2, "spline": array(POINT2),
                   "center": POINT2, "through": POINT2, "tangent": {"type": "boolean"}},
    "oneOf": [{"required": ["line"]}, {"required": ["arc"]}, {"required": ["spline"]}],
    "additionalProperties": False,
}
ROUND_SECTION = {"type": "object", "properties": {"center_mm": POINT2, "diameter_mm": POSITIVE},
                 "required": ["center_mm", "diameter_mm"], "additionalProperties": False}

# Keys and limits come from solidworks-mcp 0.19.0 server/session contracts.
RULES: dict[str, dict[str, dict]] = {
    "add_box": {key: POSITIVE for key in ("width_mm", "height_mm", "depth_mm")},
    "add_disc": {"diameter_mm": POSITIVE, "thickness_mm": POSITIVE},
    "add_cylinder": {"diameter_mm": POSITIVE, "height_mm": POSITIVE},
    "add_cone": {"bottom_diameter_mm": POSITIVE, "top_diameter_mm": NONNEGATIVE, "height_mm": POSITIVE},
    "add_revolved_profile": {"profile_mm": POLYGON2, "axis_mm": array(POINT2, 2, 2),
                             "angle_deg": {"type": "number", "exclusiveMinimum": 0, "maximum": 360},
                             "corner_radii_mm": RADII},
    "add_swept_pipe": {"path_mm": array(POINT2, 2), "diameter_mm": POSITIVE, "bend_radius_mm": NUMBER},
    "add_swept_profile": {"profile_mm": POLYGON2, "path_mm": array(POINT2, 2),
                          "bend_radius_mm": NUMBER, "corner_radii_mm": RADII},
    "add_lofted_solid": {"profiles_mm": array({"anyOf": [POLYGON2, ROUND_SECTION]}, 2),
                         "heights_mm": array(NUMBER, 2)},
    "add_rib": {"start_mm": POINT2, "end_mm": POINT2, "toward_mm": POINT2,
                "thickness_mm": POSITIVE, "z_mm": NONNEGATIVE},
    "add_thread": {"length_mm": POSITIVE},
    "add_hole": {"diameter_mm": POSITIVE},
    "add_counterbore_hole": {"clearance_diameter_mm": POSITIVE, "cbore_diameter_mm": POSITIVE,
                             "cbore_depth_mm": POSITIVE},
    "add_hole_on_face": {"diameter_mm": POSITIVE, "depth_mm": POSITIVE},
    "add_hole_wizard": {"depth_mm": POSITIVE},
    "add_boss_on_face": {"diameter_mm": POSITIVE, "height_mm": POSITIVE},
    "add_text_on_face": {"height_mm": POSITIVE, "depth_mm": POSITIVE},
    "cut_offset_pocket": {"rim_mm": POSITIVE, "depth_mm": POSITIVE},
    "cut_slot": {"length_mm": POSITIVE, "width_mm": POSITIVE, "depth_mm": POSITIVE},
    "add_extruded_slot": {"start_mm": POINT2, "end_mm": POINT2, "width_mm": POSITIVE, "depth_mm": POSITIVE},
    "extrude_sketch": {"depth_mm": POSITIVE},
    "cut_sketch": {"depth_mm": POSITIVE},
    "add_sketch": {"start_mm": POINT2, "segments": array(SEGMENT)},
    "add_fillet": {"radius_mm": POSITIVE},
    "add_chamfer": {"distance_mm": POSITIVE,
                    "angle_deg": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 90}},
    "add_linear_pattern": {"count": {"type": "integer", "minimum": 2}, "spacing_mm": POSITIVE},
    "add_circular_pattern": {"count": {"type": "integer", "minimum": 2}},
    "add_shell": {"thickness_mm": POSITIVE},
    "set_appearance": {"rgb": array({"type": "number", "minimum": 0, "maximum": 255}, 3, 3),
                       "transparency": {"type": "number", "minimum": 0, "maximum": 1}},
    "set_material": {"density_kg_m3": POSITIVE},
    "list_edges": {"within_mm": array(POINT3, 2, 2)},
    "check_printability": {"overhang_deg": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 90},
                           "min_wall_mm": POSITIVE},
    "export": {"deviation_mm": POSITIVE, "angle_deg": POSITIVE},
    "screenshot": {"zoom_mm": array(POINT3, 2, 2), "from_dir": POINT3},
    "measure_distance": {"point_mm": POINT3, "axis_mm": array(POINT3, 2, 2)},
    "check_motion": {"values": array(NUMBER), "distances": array(array({"type": "string"}, 2, 2), 0)},
    "swept_region": {"values": array(NUMBER), "heights_mm": array(NUMBER),
                     "margin_mm": NONNEGATIVE, "tolerance_mm": POSITIVE},
    "slice_mesh": {"heights_mm": array(NUMBER, 0)},
    "compare_with_mesh": {"heights_mm": array(NUMBER, 0), "offset_mm": POINT3},
}
for _name, _points in (
    ("add_extruded_profile", POLYGON2), ("add_extruded_spline", POLYGON2),
    ("add_extruded_profile_on_plane", POLYGON_PLANE), ("add_extruded_profile_on_face", POLYGON3),
    ("cut_profile", POLYGON2), ("cut_profile_on_face", POLYGON3),
    ("cut_profile_through_plane", POLYGON_PLANE),
):
    RULES[_name] = {"points_mm": _points, "depth_mm": POSITIVE, "corner_radii_mm": RADII,
                    "about_mm": POINT2,
                    "draft_deg": {"type": "number", "exclusiveMinimum": -90, "exclusiveMaximum": 90}}


def _allows_null(schema: dict) -> bool:
    kind = schema.get("type")
    return kind == "null" or (isinstance(kind, list) and "null" in kind) or any(
        _allows_null(branch) for union in ("anyOf", "oneOf") for branch in schema.get(union, [])
    )


def augment_schema(name: str, schema: dict) -> dict:
    """Tighten known geometry contracts, preserving unknown fields and nullability."""
    result = deepcopy(schema)
    properties = result.get("properties", {})
    for field, rule in RULES.get(name, {}).items():
        if field not in properties:
            continue
        old = properties[field]
        constraint = {"anyOf": [deepcopy(rule), {"type": "null"}]} if _allows_null(old) else deepcopy(rule)
        properties[field] = {**old, "allOf": [*old.get("allOf", []), constraint]}
    if name in FILE_WRITE_TOOLS:
        properties["overwrite"] = {"type": "boolean", "default": False,
            "description": "Explicitly permit replacing existing output. Default refuses an existing target."}
    return result


def _json_copy(value, *, max_bytes: int | None = MAX_ARGUMENT_BYTES):
    def visit(item, path="arguments", depth=0):
        if depth > 64:
            raise ValueError(f"{path}: JSON nesting exceeds 64 levels")
        if item is None or type(item) in (str, bool):
            return
        if type(item) in (int, float):
            try:
                finite = math.isfinite(float(item))
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{path}: numeric value must be finite and representable as a COM double")
            return
        if type(item) is list:
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]", depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError(f"{path}: object keys must be strings")
                visit(child, f"{path}.{key}", depth + 1)
            return
        raise ValueError(f"{path}: expected JSON value, got {type(item).__name__}")

    visit(value)
    text = json.dumps(value, allow_nan=False, ensure_ascii=True)
    if max_bytes is not None and len(text) > max_bytes:
        raise ValueError("Arguments exceed the 2 MiB input limit")
    return json.loads(text)


def normalize_args(name: str, arguments: dict) -> dict:
    """Detach inputs, reject nonfinite numeric values, check documented relationships.

    Schema validation must also be applied: numeric strings in ordinary text
    are legitimate, whereas strings/bools in point arrays are schema errors.
    """
    args = _json_copy(arguments)
    if type(args) is not dict:
        raise ValueError("arguments must be a JSON object")
    if name in FILE_WRITE_TOOLS and "path" in args:
        path = args["path"]
        if type(path) is not str or not path or "\x00" in path:
            raise ValueError("path must be a nonempty file path without NUL characters")
        suffix = {"save_part": ".sldprt", "save_assembly": ".sldasm"}.get(name)
        if suffix and not path.lower().endswith(suffix):
            path += suffix
        args["path"] = os.path.abspath(path)
    if name in RULES or name in {"add_mate", "set_dimension"}:
        for field in ("face", "face_a", "face_b", "up_to"):
            selector = args.get(field)
            if isinstance(selector, str) and selector.strip().startswith("@"):
                try:
                    coords = [float(value) for value in selector.strip()[1:].split(",")]
                    valid = len(coords) == 3 and all(math.isfinite(value) for value in coords)
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError(f"{field}: coordinate selector requires three finite numbers: @x,y,z")
    if name == "add_cone" and all(type(args.get(key)) in (int, float) for key in ("top_diameter_mm", "bottom_diameter_mm")):
        if args["top_diameter_mm"] >= args["bottom_diameter_mm"]:
            raise ValueError("top_diameter_mm must be smaller than bottom_diameter_mm")
    if name == "add_counterbore_hole" and all(type(args.get(key)) in (int, float) for key in ("cbore_diameter_mm", "clearance_diameter_mm")):
        if args["cbore_diameter_mm"] <= args["clearance_diameter_mm"]:
            raise ValueError("cbore_diameter_mm must exceed clearance_diameter_mm")
    if name == "add_lofted_solid":
        profiles, heights = args.get("profiles_mm"), args.get("heights_mm")
        if isinstance(profiles, list) and isinstance(heights, list):
            if len(profiles) != len(heights):
                raise ValueError("profiles_mm and heights_mm must have the same length")
            if heights and all(type(value) in (int, float) for value in heights):
                if heights[0] != 0 or any(hi <= lo for lo, hi in zip(heights, heights[1:])):
                    raise ValueError("heights_mm must start at zero and strictly increase")
    if name == "extrude_sketch":
        depth, up_to = args.get("depth_mm"), args.get("up_to")
        if (depth is None) == (up_to is None):
            raise ValueError("extrude_sketch requires depth_mm or up_to, exactly one")
    if name == "set_material" and args.get("density_kg_m3") is not None and args.get("database"):
        raise ValueError("Give a material database or density_kg_m3, not both")
    return args


def guard_native_write(name: str, arguments: dict) -> dict:
    """Consume PRO overwrite and recheck the actual target immediately before COM.

    The caller holds CadLease. This protects cooperating PRO servers; an
    unrelated process can still race the filesystem check/COM SaveAs operation.
    """
    args = deepcopy(arguments)
    if name not in FILE_WRITE_TOOLS:
        return args
    overwrite = args.pop("overwrite", False)
    if type(overwrite) is not bool:
        raise ValueError("overwrite must be a boolean")
    target = Path(args["path"])
    if target.is_dir():
        raise ValueError("OUTPUT_IS_DIRECTORY: output path names an existing directory")
    if not overwrite and (target.exists() or target.is_symlink()):
        raise ValueError("OUTPUT_EXISTS: choose a new path or explicitly pass overwrite=true")
    # SolidWorks chooses per-component filenames; checking only `path` misses
    # collisions with the parts it writes alongside that path.
    if name == "export" and args.get("per_component") and not overwrite:
        if target.parent.exists() and any(target.parent.iterdir()):
            raise ValueError("OUTPUT_DIRECTORY_NOT_EMPTY: per-component export needs an empty directory or overwrite=true")
    return args


def validate_result(result: dict) -> dict:
    """Turn reported broken geometry into an explicit stop signal; never undo it."""
    result = _json_copy(result, max_bytes=None)
    if type(result) is not dict:
        raise ValueError("Native result must be a JSON object")
    if result.get("ok") is False or result.get("isError") is True:
        return result
    reasons = []
    if result.get("rebuild_ok") is False:
        reasons.append("SolidWorks reported rebuild_ok=false")
    if result.get("broken_features"):
        reasons.append("SolidWorks reported broken_features")
    for field in ("failing_features", "mate_errors", "broken_equations"):
        entries = result.get(field)
        if not entries:
            continue
        # Mate reports nest the native warning flag under `error`; feature
        # reports place it directly on the entry. Warnings do not stop a plan.
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("error", entry), dict)
            or entry.get("error", entry).get("warning") is not True
            for entry in entries
        ):
            reasons.append(f"SolidWorks reported {field}")
    if reasons:
        result.update(ok=False, error_code="CAD_POSTCONDITION_FAILED",
                      error="; ".join(reasons) + ". Inspect/correct the model before continuing; changes remain.",
                      cad_reported_ok=result.get("ok"), changes_may_remain=True)
    return result
