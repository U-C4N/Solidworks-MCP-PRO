"""Source-verified competitor calls; constructing these mappings never runs CAD.

Arguments are mm and degrees unless stated. The live harness owns document
selection and must pass back actual document/feature/dimension names. No source
patches or hidden retries are performed by these adapters.
"""
from __future__ import annotations

from pathlib import Path


def request(server: str, operation: str, **p) -> dict:
    """Translate a benchmark operation to an upstream MCP name/argument pair.

    Supported operations: new_part, sketch_start, rectangle, circle,
    sketch_finish, extrude, cut_through, mass, bounds, dimensions,
    set_dimension, export, screenshot, save, close. Unknown operations fail.
    """
    if server == "harrier":
        table = {
            "new_part": ("solidworks_new_part", {}),
            "sketch_start": ("solidworks_create_sketch", {"plane": p.get("plane", "Front")}),
            "rectangle": ("solidworks_sketch_rectangle", {
                "width": p.get("width", 80), "height": p.get("height", 60),
                "centerX": p.get("center_x", 0), "centerY": p.get("center_y", 0)}),
            "circle": ("solidworks_sketch_circle", {
                "centerX": p.get("center_x", 0), "centerY": p.get("center_y", 0),
                "radius": p.get("radius", 5)}),
            "sketch_finish": ("solidworks_exit_sketch", {}),
            "extrude": ("solidworks_create_extrusion", {"depth": p.get("depth", 10)}),
            "cut_through": ("solidworks_create_cut_extrusion", {
                "depth": p.get("depth", 10), "endCondition": "THROUGH_ALL"}),
            "mass": ("solidworks_get_mass_properties", {}),
            "bounds": ("solidworks_get_body_info", {}),
            "dimensions": ("solidworks_list_parameters", {}),
            "set_dimension": ("solidworks_set_parameter", {
                "parameter": p.get("dimension"), "value": p.get("value"), "unit": "mm"}),
            "export": ("solidworks_save_document", {"path": p.get("path")}),
            "save": ("solidworks_save_document", {"path": p.get("path")}),
            "screenshot": ("solidworks_look_at_model", {"view": "isometric", "labels": False}),
            "close": ("solidworks_close_document", {"name": p.get("doc")}),
        }
    elif server == "soheil":
        target = {"doc": p["doc"]} if p.get("doc") else {}
        table = {
            "new_part": ("doc_new", {"type": "part", "units": "mmgs", "name": p.get("name")}),
            "sketch_start": ("sketch_start", {"on": {"name": p.get("plane", "front")}}),
            "rectangle": ("sketch_add", {"entities": [{"kind": "rectangle",
                "center": [p.get("center_x", 0), p.get("center_y", 0)],
                "widthMm": p.get("width", 80), "heightMm": p.get("height", 60)}]}),
            "circle": ("sketch_add", {"entities": [{"kind": "circle",
                "center": [p.get("center_x", 0), p.get("center_y", 0)],
                "radiusMm": p.get("radius", 5)}]}),
            "sketch_finish": ("sketch_finish", {}),
            "extrude": ("feature_extrude", {"depthMm": p.get("depth", 10)}),
            "cut_through": ("feature_extrude", {"cut": True, "end": "through_all"}),
            "mass": ("model_mass", {}),
            "bounds": ("body_list", {}),
            "dimensions": ("dimension_list", {}),
            "set_dimension": ("dimension_set", {"values": [
                {"name": p.get("dimension"), "value": p.get("value")}]}),
            "export": ("model_export", {"path": p.get("path")}),
            "save": ("doc_save", {"mode": "as", "path": p.get("path")}),
            "screenshot": ("view_screenshot", {"view": "iso", "file": p.get("path")}),
            "close": ("doc_close", {"discard": True}),
        }
        if operation != "new_part" and operation in table:
            table[operation][1].update(target)
    elif server == "andrew":
        # Native mode is a separate launch from manifest discovery's --mock.
        cx, cy = p.get("center_x", 0), p.get("center_y", 0)
        width, height = p.get("width", 80), p.get("height", 60)
        fmt = Path(p.get("path", "output.step")).suffix.lower().lstrip(".")
        fmt = "step" if fmt in {"step", "stp"} else fmt
        table = {
            "new_part": ("create_part", {"name": p.get("name"), "units": "mm"}),
            "sketch_start": ("create_sketch", {"plane": p.get("plane", "Front")}),
            "rectangle": ("add_rectangle", {"x1": cx-width/2, "y1": cy-height/2,
                                                "x2": cx+width/2, "y2": cy+height/2}),
            "circle": ("add_circle", {"center_x": cx, "center_y": cy, "radius": p.get("radius", 5)}),
            "sketch_finish": ("exit_sketch", {}),
            "extrude": ("create_extrusion", {"sketch_name": p.get("sketch"), "depth": p.get("depth", 10)}),
            "cut_through": ("create_cut_extrude", {"sketch_name": p.get("sketch"),
                "depth": p.get("depth", 10), "end_condition": "ThroughAll"}),
            "mass": ("get_mass_properties", {}),
            "bounds": ("verify_model", {}),
            "dimensions": ("list_model_dimensions", {}),
            "set_dimension": ("set_dimension", {"name": p.get("dimension"), "value": p.get("value")}),
            "export": ("export_stl" if fmt == "stl" else "export_step", {
                "file_path": str(p.get("path", "")), "format_type": fmt}),
            "save": ("save_as", {"file_path": str(p.get("path", "")), "overwrite": False}),
            "screenshot": ("export_image", {"file_path": str(p.get("path", "")), "format_type": "png",
                "view_orientation": "isometric", "width": 1024, "height": 768}),
            "close": ("close_model", {"save": False}),
        }
        if operation == "new_part" and not p.get("name"):
            raise ValueError("Andrew requires a unique new document name")
        if operation in {"extrude", "cut_through"} and not p.get("sketch"):
            raise ValueError("Use the actual sketch name returned by upstream")
        if operation not in {"sketch_finish", "mass", "bounds", "dimensions"} and operation in table:
            name, data = table[operation]
            table[operation] = (name, {"input_data": data})
    else:
        raise ValueError(f"No source-verified live adapter for {server!r}")
    if operation not in table:
        raise ValueError(f"Unsupported operation {operation!r}")
    if operation in {"set_dimension"} and (not p.get("dimension") or "value" not in p):
        raise ValueError("Use a dimension name returned by the upstream catalog")
    if operation in {"save", "export"} and not p.get("path"):
        raise ValueError("An isolated output path is required")
    if operation == "close" and not p.get("doc"):
        raise ValueError("Closing requires the exact document created by this benchmark")
    name, arguments = table[operation]
    return {"name": name, "arguments": {k: str(v) if isinstance(v, Path) else v
            for k, v in arguments.items() if v is not None}}


SOURCE_NOTES = {
    "harrier": {
        "revision": "60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea",
        "files": ["solidworks/modeling.py", "solidworks/sketching.py",
                  "solidworks/document_manager.py", "solidworks/geometry_query.py"],
        "cut_retry": "Upstream internally retries a failed cut with opposite direction once.",
        "errors": "Upstream returns some errors as text beginning with a cross, not isError.",
        "close": "Unconditionally discards unsaved changes; benchmark must pass its own exact document title.",
    },
    "soheil": {
        "revision": "79fb529b67ca4a0587e5570024a772fad0b2c14d",
        "files": ["src/SolidWorksMcp.Server/Tools/DocumentTools.cs", "src/SolidWorksMcp.Server/Tools/SketchTools.cs",
                  "src/SolidWorksMcp.Server/Tools/FeatureTools.cs", "src/SolidWorksMcp.Server/Tools/FeatureManagementTools.cs",
                  "src/SolidWorksMcp.Server/Tools/MeasureTools.cs", "src/SolidWorksMcp.Server/Tools/ExportTools.cs",
                  "src/SolidWorksMcp.Server/Tools/ViewTools.cs"],
        "close": "Server enforces ownership; discard only for documents created by this server.",
    },
    "andrew": {
        "revision": "126beed0e7a13214ebdd5724929f4c095cf2642f",
        "files": ["src/solidworks_mcp/tools/modeling.py", "src/solidworks_mcp/tools/sketching.py",
                  "src/solidworks_mcp/tools/analysis.py", "src/solidworks_mcp/tools/export.py",
                  "src/solidworks_mcp/tools/file_management.py"],
        "runtime": "Manifest uses upstream --mock for discovery; remove it only for a serialized native CAD run.",
        "close": "Tool targets the active document only; caller must verify it is its own exact document before close.",
    },
}
