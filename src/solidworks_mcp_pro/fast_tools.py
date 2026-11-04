"""Small public contracts for server-side workflows and on-demand results."""
from mcp import types


COMPACT_TOOLS = frozenset({
    "pro_capabilities", "pro_diagnose", "pro_repair", "pro_colorize", "pro_render_job",
    "pro_operation", "pro_operations", "pro_result", "pro_run_plan", "pro_tool_help", "pro_connection_doctor",
    "pro_validate_design", "pro_variant_study", "pro_shot_pack",
})
NEW_WORKFLOWS = frozenset({"pro_diagnose", "pro_repair", "pro_colorize", "pro_render_job",
                           "pro_validate_design", "pro_variant_study", "pro_shot_pack"})
VIEW = {"type": "string", "enum": ["summary", "full"]}
STRING = {"type": "string", "minLength": 1, "maxLength": 4096}
REQUEST = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"}


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


RGB_ASSIGNMENTS = {"type": "array", "minItems": 1, "maxItems": 128, "items": object_schema({
    "component": STRING,
    "color_rgb": {"type": "array", "minItems": 3, "maxItems": 3,
                  "items": {"type": "integer", "minimum": 0, "maximum": 255}},
}, ("color_rgb",))}
TRANSFORMS = {"type": "array", "minItems": 1, "maxItems": 128, "items": object_schema({
    "component": STRING,
    "position_mm": {"type": "array", "minItems": 3, "maxItems": 3,
                    "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}},
    "rotation_deg": {"type": "array", "minItems": 3, "maxItems": 3,
                     "items": {"type": "number", "minimum": -3600, "maximum": 3600}},
}, ("component", "position_mm", "rotation_deg"))}


def fast_tools():
    definitions = [
        ("pro_diagnose", "Read-only health scan of the exact active saved model. Reports errors, warnings and unchecked scopes. Deep checks are optional; never repairs.", object_schema({
            "document_path": STRING, "deep": {"type": "boolean", "default": False}, "result_view": VIEW,
        }, ("document_path",)), True),
        ("pro_repair", "Plan repairs first without changes. Only a separate action=apply executes that plan after stale-state checks. No guessed dimensions, equations or mates.", object_schema({
            "action": {"type": "string", "enum": ["plan", "apply"], "default": "plan"},
            "document_path": STRING, "plan_id": STRING,
            "actions": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                        "items": {"type": "string", "enum": ["reconnect", "resolve", "rebuild"]}},
            "components": {"type": "array", "minItems": 1, "maxItems": 128, "uniqueItems": True,
                           "items": STRING}, "result_view": VIEW,
        }), False),
        ("pro_colorize", "Batch RGB on the exact active saved part or named top-level assembly components. Preflights all targets, reads back colors, redraws once. No save or rebuild.", object_schema({
            "document_path": STRING, "assignments": RGB_ASSIGNMENTS, "request_id": REQUEST,
            "dry_run": {"type": "boolean", "default": False}, "result_view": VIEW,
        }, ("document_path", "assignments", "request_id")), False),
        ("pro_render_job", "One job: diagnose, optional RGB, explicit explosion, framing, preview then final. Restores temporary poses/camera; leaves requested colors unsaved. Poll operation_id; do not resubmit.", object_schema({
            "document_path": STRING, "request_id": REQUEST,
            "mode": {"type": "string", "enum": ["normal", "exploded"], "default": "normal"},
            "transforms": TRANSFORMS, "assignments": RGB_ASSIGNMENTS,
            "view": {"type": "string", "enum": ["current", "front", "back", "left", "right", "top", "bottom", "isometric", "dimetric", "trimetric"], "default": "isometric"},
            "quality": {"type": "string", "enum": ["standard", "4k"], "default": "standard"},
            "width": {"type": "integer", "minimum": 64, "maximum": 4096},
            "height": {"type": "integer", "minimum": 64, "maximum": 4096},
            "passes": {"type": "integer", "minimum": 1, "maximum": 5000},
            "denoiser": {"type": "boolean", "default": True}, "result_view": VIEW,
        }, ("document_path", "request_id")), False),
        ("pro_result", "Retrieve an existing operation's full evidence or one image without rerunning CAD. Polling is image-free; use kind=image once for delivery.", object_schema({
            "operation_id": STRING,
            "kind": {"type": "string", "enum": ["details", "image"], "default": "details"},
            "artifact_id": STRING,
        }, ("operation_id",)), True),
        ("pro_tool_help", "Get an exact native or PRO tool schema on demand. Native tools can be used through pro_run_plan; PRO workflows are not plan steps. No CAD call.", object_schema({
            "tool": STRING,
        }, ("tool",)), True),
    ]
    result = [types.Tool(name=name, description=description, inputSchema=schema,
                        annotations=types.ToolAnnotations(readOnlyHint=read_only,
                            destructiveHint=not read_only, idempotentHint=read_only, openWorldHint=False))
              for name, description, schema, read_only in definitions]
    repair = next(t for t in result if t.name == "pro_repair")
    repair.inputSchema["allOf"] = [{
        "if": {"properties": {"action": {"const": "apply"}}, "required": ["action"]},
        "then": {"required": ["plan_id"], "not": {"anyOf": [
            {"required": ["document_path"]}, {"required": ["actions"]}, {"required": ["components"]}]}},
        "else": {"required": ["document_path"], "not": {"required": ["plan_id"]}},
    }]
    render = next(t for t in result if t.name == "pro_render_job")
    render.inputSchema["allOf"] = [{
        "if": {"properties": {"mode": {"const": "exploded"}}, "required": ["mode"]},
        "then": {"required": ["transforms"]}, "else": {"not": {"required": ["transforms"]}},
    }]
    return result
