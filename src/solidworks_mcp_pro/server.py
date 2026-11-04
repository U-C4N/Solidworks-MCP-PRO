"""Standards-based stdio MCP server: pinned CAD tools plus PRO workflows."""
from __future__ import annotations

import argparse
import asyncio
import base64
from importlib import metadata, resources
import json
import math
from pathlib import Path
import platform
from typing import Callable

from jsonschema import Draft202012Validator, ValidationError
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from . import __version__
from .backend import NativeBackend
from .operations import OperationRegistry
from .storage import ArtifactStore, CadLease, default_workspace
from .visualization import build_viewer, inspect_stl, render_svg
from .workflow import WorkflowEngine
from .validation import augment_schema, normalize_args
from .fast_tools import COMPACT_TOOLS, NEW_WORKFLOWS, VIEW, fast_tools
from .release_tools import release_tools


READ_ONLY = {
    "get_status", "list_documents", "list_dimensions", "read_sketch", "list_features",
    "list_equations", "get_mass_properties", "get_bounding_box", "list_faces", "list_edges",
    "list_bodies", "list_components", "get_assembly_bounding_box", "measure_distance",
    "check_interference", "check_printability", "slice_mesh", "compare_with_mesh",
}


def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


STRING = {"type": "string", "minLength": 1, "maxLength": 4096}
WAIT = {"type": "number", "minimum": 0, "maximum": 20, "default": 0}


def pro_tools() -> list[types.Tool]:
    definitions = [
        ("pro_capabilities", "Report exact versions, tools, units and verification limits. No CAD required.", {}, (), True),
        ("pro_run_plan", "Validate an entire native CAD plan before execution. Dry run defaults true. request_id prevents repeat execution in this process. Partial successes remain on failure; no plan rollback.", {
            "steps": {"type": "array", "minItems": 1, "maxItems": 32, "items": _schema({"tool": STRING, "arguments": {"type": "object"}}, ("tool", "arguments"))},
            "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"},
            "dry_run": {"type": "boolean", "default": True}}, ("steps", "request_id"), False),
        ("pro_operations", "List running/recent operations without calling COM. Use after timeout or cancellation.", {}, (), True),
        ("pro_operation", "Poll an operation; use wait_seconds=20 to avoid frequent polling. A client timeout does not cancel CAD/render; do not resubmit.", {"operation_id": STRING, "wait_seconds": WAIT, "result_view": VIEW,
            "include_images": {"type": "boolean"}}, ("operation_id",), True),
        ("pro_audit_tail", "Return recent plan audit entries; records omit full CAD arguments.", {"limit": {"type": "integer", "minimum": 1, "maximum": 256, "default": 20}}, (), True),
        ("pro_inspect_stl", "Inspect local STL bounds/topology/volume. STL has no units: coordinates assumed mm. No SolidWorks required.", {"path": STRING}, ("path",), True),
        ("pro_preview_stl", "Make an offline orbit/zoom/wireframe HTML viewer or SVG from a local STL. This is a mesh preview, not Visualize photorealistic rendering.", {"path": STRING, "format": {"type": "string", "enum": ["html", "svg"], "default": "html"}, "title": STRING}, ("path",), False),
        ("pro_preview_model", "Export current CAD model to STL and create a standalone offline 3D viewer.", {"title": STRING}, (), False),
        ("pro_capture_view", "Capture current CAD model and return actual PNG image content to Codex/Claude.", {"view": {"type": "string", "enum": ["iso", "front", "back", "left", "right", "top", "bottom"], "default": "iso"}}, (), False),
        ("pro_verify_volume", "Compare measured CAD volume with an independent expected value in mm3.", {"expected_mm3": {"type": "number", "exclusiveMinimum": 0}, "relative_tolerance": {"type": "number", "minimum": 0, "maximum": 0.1, "default": 0.001}}, ("expected_mm3",), True),
        ("pro_visualize_status", "Detect SOLIDWORKS Visualize add-in API (2025+). Explicit load_if_missing=true loads the registered Visualize add-in for this session, then probes it.", {"load_if_missing": {"type": "boolean", "default": False}}, (), False),
        ("pro_visualize_render", "Photorealistic render via the official SOLIDWORKS Visualize add-in API. Returns operation_id immediately: poll pro_operation. Uses current scene/materials; requires a licensed installed Visualize add-in. Never repeat a running request_id.", {
            "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"},
            "width": {"type": "integer", "minimum": 64, "maximum": 4096, "default": 1280},
            "height": {"type": "integer", "minimum": 64, "maximum": 4096, "default": 720},
            "passes": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 250},
            "denoiser": {"type": "boolean", "default": False},
            "image_format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"}}, ("request_id",), False),
        ("pro_visualize_export", "Export current CAD to a native .SVPJ Visualize project. Grouping requires Visualize 2026 API.", {"grouping": {"type": "string", "enum": ["appearance", "part", "none"], "default": "part"}}, (), False),
        ("pro_visualize_render_project", "Render a native Visualize project through the installed Offline Renderer. Optionally pin its active camera to a reference project and adjust ground shadows/reflections on a new copy. Source projects remain unchanged. Poll pro_operation; do not repeat a running request_id.", {
            "request_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"},
            "project_path": STRING, "reference_project_path": STRING,
            "camera_position_mm": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}},
            "camera_target_mm": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}},
            "vertical_fov_deg": {"type": "number", "minimum": 1, "maximum": 150},
            "ground_reflections": {"type": "boolean", "default": False},
            "ground_shadows": {"type": "boolean", "default": True},
            "width": {"type": "integer", "minimum": 128, "maximum": 4096, "default": 1920},
            "height": {"type": "integer", "minimum": 128, "maximum": 4096, "default": 1080},
            "passes": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 32},
            "denoiser": {"type": "boolean", "default": True}}, ("request_id", "project_path"), False),
        ("pro_visualize_open_project", "Request opening an existing local .SVPJ project through the native SOLIDWORKS Visualize 2026+ API, without UI automation. Reports API acceptance only; project loading is not independently verified.", {"path": STRING}, ("path",), False),
        ("pro_visualize_appearances", "Apply installed .p2m appearances to exact named components of the specified saved assembly. Validates every target before mutation; does not save the assembly or change its scene.", {
            "document_path": STRING,
            "dry_run": {"type": "boolean", "default": False},
            "assignments": {"type": "array", "minItems": 1, "maxItems": 128,
                "items": _schema({"component": STRING, "material_path": STRING,
                    "color_rgb": {"type": "array", "minItems": 3, "maxItems": 3,
                        "items": {"type": "integer", "minimum": 0, "maximum": 255}}},
                    ("component", "material_path"))}}, ("document_path", "assignments"), False),
        ("pro_visualize_scene", "Inspect or configure the exact saved model's native scene/HDRI, environment rotation, floor shadows/reflections and exact named lights. dry_run=true inventories current scene and light names without applying changes. Does not save. Visualize fidelity requires a rendered check.", {
            "document_path": STRING, "scene_path": STRING, "hdri_path": STRING,
            "environment_rotation_deg": {"type": "number", "minimum": 0, "maximum": 360},
            "background_type": {"type": "string", "enum": ["environment", "plain", "gradient"],
                "description": "Explicit backdrop mode. Overrides the environment default selected by hdri_path."},
            "background_top_rgb": {"type": "array", "minItems": 3, "maxItems": 3,
                "items": {"type": "integer", "minimum": 0, "maximum": 255},
                "description": "RGB for the effective plain or gradient background."},
            "background_bottom_rgb": {"type": "array", "minItems": 3, "maxItems": 3,
                "items": {"type": "integer", "minimum": 0, "maximum": 255},
                "description": "Bottom RGB for the effective gradient background only."},
            "floor_shadows": {"type": "boolean"}, "floor_reflections": {"type": "boolean"},
            "disable_light_names": {"type": "array", "minItems": 0, "maxItems": 64,
                "uniqueItems": True, "items": STRING},
            "disable_light_ids": {"type": "array", "minItems": 0, "maxItems": 64,
                "uniqueItems": True, "items": {"type": "integer", "minimum": 0, "maximum": 255},
                "description": "Exact native IDs from a fresh scene inspection; use instead of names when names are duplicated."},
            "dry_run": {"type": "boolean", "default": False}}, ("document_path",), False),
        ("pro_visualize_camera", "Inspect or apply the exact saved model's active view orientation, framing and perspective for Visualize. This configures the model view, not a named camera entity or lens. No save. For custom orientation, from_direction points from model toward camera and up_direction is the desired up axis.", {
            "document_path": STRING,
            "action": {"type": "string", "enum": ["inspect", "apply"], "default": "inspect"},
            "view": {"type": "string", "enum": ["current", "front", "back", "left", "right", "top", "bottom", "isometric", "dimetric", "trimetric", "custom"], "default": "isometric"},
            "from_direction": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number"}},
            "up_direction": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number"}},
            "fit": {"type": "boolean", "default": True},
            "zoom_factor": {"type": "number", "minimum": 0.25, "maximum": 2, "default": 1},
            "perspective": {"type": "boolean", "default": False},
            "object_sizes_away": {"type": "number", "minimum": 1, "maximum": 20, "default": 3,
                "description": "Perspective distance in object sizes, used only when perspective is true; not millimetres."}}, ("document_path",), False),
        ("pro_assembly_configurations", "Inspect native assembly and component configurations of the exact saved assembly. No mutations.", {"document_path": STRING}, ("document_path",), True),
        ("pro_visualize_named_camera", "Inspect, create or update an owned native camera with absolute position/target in millimetres. Can activate, deactivate or delete only owned MCP cameras. Lens fields are readback-only. No save; verify actual Visualize export framing.", {
            "document_path": STRING,
            "action": {"type": "string", "enum": ["inspect", "apply", "deactivate", "delete"], "default": "inspect"},
            "name": {"type": "string", "pattern": "^MCP_[A-Za-z0-9_-]{1,80}$", "default": "MCP_Video"},
            "position_mm": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}},
            "target_mm": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}},
            "roll_deg": {"type": "number", "minimum": -3600, "maximum": 3600, "default": 0},
            "activate": {"type": "boolean", "default": True}}, ("document_path",), False),
        ("pro_sheet_bend", "Create or update an owned geometric sheet-bending configuration in an exact saved part copy. Constant thickness, circular bends and conserved developed length; native solid volume is verified. This is geometric animation, not forming simulation. No save.", {
            "document_path": STRING,
            "angle_deg": {"type": "number", "anyOf": [{"const": 0}, {"minimum": 0.01, "maximum": 90}]},
            "bottom_offset_mm": {"type": "number", "minimum": -1000, "maximum": 1000, "default": 0},
            "configuration": {"type": "string", "pattern": "^MCP_Bend[A-Za-z0-9_-]{0,80}$", "default": "MCP_Bend"},
            "feature_name": {"type": "string", "pattern": "^MCP_Bend[A-Za-z0-9_-]{0,80}$", "default": "MCP_BendBody"},
            "suppress_features": {"type": "array", "minItems": 1, "maxItems": 64, "uniqueItems": True,
                                  "items": {"type": "string", "minLength": 1, "maxLength": 256}},
            "length_mm": {"type": "number", "minimum": 0.1, "maximum": 10000, "default": 90},
            "depth_mm": {"type": "number", "minimum": 0.1, "maximum": 10000, "default": 80},
            "thickness_mm": {"type": "number", "minimum": 0.01, "maximum": 1000, "default": 1.5},
            "flat_mm": {"type": "number", "minimum": 0.1, "maximum": 10000, "default": 36},
            "inner_radius_mm": {"type": "number", "minimum": 0.01, "maximum": 1000, "default": 2}},
            ("document_path", "angle_deg", "suppress_features"), False),
        ("pro_assembly_visibility", "Hide or show exact top-level components for staged assembly renders. Native readback and rollback on failure; no rebuild or save. dry_run with [] inventories visibility.", {
            "document_path": STRING, "dry_run": {"type": "boolean", "default": False},
            "components": {"type": "array", "maxItems": 128, "items": _schema({
                "component": STRING, "visible": {"type": "boolean"}}, ("component", "visible"))}},
            ("document_path", "components"), False),
        ("pro_assembly_configure", "Select an existing native assembly configuration OR exact top-level component configurations. Preflight, native readback and rollback on failure; no save.", {
            "document_path": STRING, "configuration": STRING,
            "components": {"type": "array", "minItems": 1, "maxItems": 128,
                "items": _schema({"component": STRING, "configuration": STRING}, ("component", "configuration"))}}, ("document_path",), False),
        ("pro_assembly_clone", "Create an isolated native Pack and Go copy with its referenced CAD files in a new absolute directory. Refuses existing destinations. Does not activate the copy.", {
            "document_path": STRING, "output_directory": STRING,
            "prefix": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,48}$", "default": "Animation_",
                "description": "Unique filename prefix; avoid names of documents already open in SOLIDWORKS."}}, ("document_path", "output_directory"), False),
        ("pro_assembly_pose", "Apply absolute transforms to exact top-level assembly components in a batch. Native readback and rollback on failure; no rebuild or save. Mates can override transforms on a later rebuild. dry_run=true inventories poses.", {
            "document_path": STRING, "dry_run": {"type": "boolean", "default": False},
            "transforms": {"type": "array", "maxItems": 128, "items": _schema({
                "component": STRING,
                "position_mm": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}},
                "rotation_deg": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number", "minimum": -3600, "maximum": 3600}}},
                ("component", "position_mm", "rotation_deg"))}}, ("document_path", "transforms"), False),
    ]
    result = [types.Tool(name=name, description=description, inputSchema=_schema(props, required),
                       annotations=types.ToolAnnotations(readOnlyHint=read_only,
                           destructiveHint=name in {"pro_run_plan", "pro_visualize_appearances", "pro_visualize_camera", "pro_visualize_named_camera", "pro_visualize_scene", "pro_assembly_configure", "pro_assembly_pose", "pro_assembly_visibility", "pro_sheet_bend"}, idempotentHint=read_only, openWorldHint=False))
            for name, description, props, required, read_only in definitions]
    configuration = next(tool for tool in result if tool.name == "pro_assembly_configure")
    configuration.inputSchema["oneOf"] = [{"required": ["configuration"]}, {"required": ["components"]}]
    project = next(tool for tool in result if tool.name == "pro_visualize_render_project")
    project.inputSchema["dependentRequired"] = {
        "camera_position_mm": ["camera_target_mm"], "camera_target_mm": ["camera_position_mm"],
    }
    project.inputSchema["not"] = {
        "properties": {"ground_reflections": {"const": True}, "ground_shadows": {"const": False}},
        "required": ["ground_reflections", "ground_shadows"],
    }
    named_camera = next(tool for tool in result if tool.name == "pro_visualize_named_camera")
    named_camera.inputSchema["allOf"] = [{
        "if": {"properties": {"action": {"const": "apply"}}, "required": ["action"]},
        "then": {"required": ["position_mm", "target_mm"]},
        "else": {"not": {"anyOf": [{"required": ["position_mm"]}, {"required": ["target_mm"]}]},
                 "properties": {"roll_deg": {"const": 0}, "activate": {"const": True}}},
    }]
    visibility = next(tool for tool in result if tool.name == "pro_assembly_visibility")
    visibility.inputSchema["allOf"] = [{
        "if": {"properties": {"dry_run": {"const": True}}, "required": ["dry_run"]},
        "else": {"properties": {"components": {"minItems": 1}}},
    }]
    camera = next(tool for tool in result if tool.name == "pro_visualize_camera")
    camera.inputSchema["allOf"] = [{
        "if": {"properties": {"view": {"const": "custom"}}, "required": ["view"]},
        "then": {"required": ["from_direction"]},
        "else": {"not": {"anyOf": [{"required": ["from_direction"]}, {"required": ["up_direction"]}]}},
    }]
    scene = next(tool for tool in result if tool.name == "pro_visualize_scene")
    scene.inputSchema["not"] = {
        "properties": {"floor_reflections": {"const": True}, "floor_shadows": {"const": False}},
        "required": ["floor_reflections", "floor_shadows"],
    }
    scene.inputSchema["allOf"] = [
        {"not": {
            "properties": {"disable_light_names": {"minItems": 1}, "disable_light_ids": {"minItems": 1}},
            "required": ["disable_light_names", "disable_light_ids"],
        }},
        {"not": {
            "properties": {"disable_light_ids": {"minItems": 1}},
            "required": ["scene_path", "disable_light_ids"],
        }},
        {"if": {
            "anyOf": [
                {"properties": {"background_type": {"const": "environment"}}, "required": ["background_type"]},
                {"required": ["hdri_path"], "not": {"required": ["background_type"]}},
            ]},
         "then": {"not": {"anyOf": [
             {"required": ["background_top_rgb"]}, {"required": ["background_bottom_rgb"]},
         ]}}},
        {"if": {"properties": {"background_type": {"const": "plain"}}, "required": ["background_type"]},
         "then": {"not": {"required": ["background_bottom_rgb"]}}},
        {"if": {"required": ["scene_path"], "anyOf": [
            {"required": ["background_top_rgb"]}, {"required": ["background_bottom_rgb"]},
        ]}, "then": {"required": ["background_type"]}},
    ]
    return result + fast_tools() + release_tools()


class ProApplication:
    def __init__(self, backend, workspace: Path, *, tool_profile: str = "full"):
        if tool_profile not in {"full", "compact"}:
            raise ValueError("tool_profile must be full or compact")
        self.tool_profile = tool_profile
        self.backend = backend
        self.store = ArtifactStore(workspace)
        self.registry = OperationRegistry()
        self.tools: list[types.Tool] = []
        self.schemas: dict[str, dict] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        self._native_tool_count = 0
        self._pro_tool_count = 0
        self.engine = None
        self.request_payloads: dict[str, str] = {}
        self.summary_operations: set[str] = set()
        self.detail_artifacts: dict[str, tuple[str, str]] = {}
        self.operation_stages: dict[str, dict] = {}
        self.all_tools: dict[str, types.Tool] = {}
        self.server = Server("solidworks-mcp-pro", version=__version__, instructions=(
            "SolidWorks MCP PRO extends hjbaard/solidworks-mcp 0.19.0. "
            "Read solidworks-pro://guide. All native geometry uses mm/degrees. "
            "Use pro_render_job for a diagnosed preview-and-final workflow. "
            "Use pro_connection_doctor to identify this server; pro_validate_design for explicit acceptance checks, "
            "pro_variant_study for isolated saved-part alternatives and pro_shot_pack for multi-format native renders. "
            "Poll pro_operation with wait_seconds=20; fetch one final image through pro_result. "
            "pro_repair plans first; apply only after a separate user instruction. "
            "pro_tool_help exposes exact schemas for native steps in pro_run_plan. "
            "A running operation must be polled, never retried.\n"
            + backend.instructions))

    async def initialize(self):
        native = await self.backend.list_tools()
        native_schemas = {tool.name: {**tool.inputSchema, "additionalProperties": False} for tool in native}
        for tool in native:
            # Unknown top-level kwargs are usually modelling typos, not intent.
            tool.inputSchema = augment_schema(tool.name, native_schemas[tool.name])
            tool.annotations = types.ToolAnnotations(readOnlyHint=tool.name in READ_ONLY,
                destructiveHint=tool.name not in READ_ONLY, idempotentHint=tool.name in READ_ONLY,
                openWorldHint=False)
        extensions = pro_tools()
        self._native_tool_count, self._pro_tool_count = len(native), len(extensions)
        self.all_tools = {t.name: t for t in native + extensions}
        self.tools = [t for t in self.all_tools.values()
                      if self.tool_profile == "full" or t.name in COMPACT_TOOLS]
        self.schemas = {name: t.inputSchema for name, t in self.all_tools.items()}
        for name, schema in self.schemas.items():
            # Check the immutable schemas once, then retain validators. The SDK
            # convenience validator otherwise repeats metaschema validation on
            # every wire call before our own mandatory input validation.
            Draft202012Validator.check_schema(schema)
            self._validators[name] = Draft202012Validator(schema)
        # Engine applies the same augmentation itself; supplying raw contracts
        # avoids adding identical allOf constraints to every plan step twice.
        self.engine = WorkflowEngine(native_schemas, self.backend.call,
                                     self.store.root / "audit.jsonl", execution_scope=CadLease)

        @self.server.list_tools()
        async def list_tools():
            return self.tools

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            return await self.call(name, arguments)

        @self.server.list_resources()
        async def list_resources():
            return [types.Resource(uri="solidworks-pro://guide", name="PRO guide", mimeType="text/markdown"),
                    types.Resource(uri="solidworks://guide", name="Native CAD guide", mimeType="text/markdown")] + [
                types.Resource(uri="solidworks-pro://artifact/" + key, name=key, mimeType=mime)
                for key, (_, mime) in self.store.items.items()]

        @self.server.read_resource()
        async def read_resource(uri):
            address = str(uri)
            if address == "solidworks-pro://guide":
                return [ReadResourceContents(resources.files("solidworks_mcp_pro").joinpath("guide.md").read_text(encoding="utf-8"), "text/markdown")]
            if address == "solidworks://guide":
                return [ReadResourceContents(self.backend.guide(), "text/markdown")]
            prefix = "solidworks-pro://artifact/"
            if not address.startswith(prefix):
                raise ValueError("Unknown resource")
            data, mime = self.store.read(address[len(prefix):])
            return [ReadResourceContents(data.decode("utf-8-sig" if mime == "text/csv" else "utf-8")
                if mime in ("text/html", "image/svg+xml", "application/json", "text/csv") else data, mime)]

        @self.server.list_prompts()
        async def list_prompts():
            return [types.Prompt(name="design-and-render", description="Verified CAD to Visualize rendering workflow",
                                 arguments=[types.PromptArgument(name="design", required=True)])]

        @self.server.get_prompt()
        async def get_prompt(name, arguments):
            if name != "design-and-render":
                raise ValueError("Unknown prompt")
            design = (arguments or {}).get("design", "")
            return types.GetPromptResult(messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=(
                f"Design brief: {design}\nRead both CAD guides; plan features and units, dry-run the plan, then build "
                "in a new document. Verify dimensions and expected volume, inspect a screenshot, save native CAD. "
                "Check Visualize availability. Render with pro_visualize_render; poll the operation and show "
                "the actual output image. Report any unsupported operation or missing license accurately.")))])

    async def _locked(self, factory: Callable):
        async with self.engine.execution_lock:
            with CadLease():
                return await factory()

    def _key(self, namespace: str, request_id: str, arguments: dict) -> str:
        key = namespace + ":" + request_id
        payload = json.dumps(arguments, sort_keys=True, allow_nan=False)
        if key in self.request_payloads and self.request_payloads[key] != payload:
            raise ValueError("request_id was already used with different arguments")
        if key not in self.request_payloads and len(self.request_payloads) >= 256:
            raise ValueError("Session request-id capacity reached; finish operations before restarting")
        self.request_payloads[key] = payload
        return key

    async def call(self, name: str, arguments: dict) -> types.CallToolResult:
        try:
            if name not in self.schemas:
                raise ValueError("Unknown tool: " + name)
            # This is the single input-validation boundary for both MCP wire
            # requests and in-process callers. Never bypass normalization or
            # instance validation: only schema compilation is cached.
            arguments = normalize_args(name, arguments)
            self._validators[name].validate(arguments)
            data = await self._dispatch(name, arguments)
            operation_id = data.get("operation_id")
            if operation_id and (name in NEW_WORKFLOWS or self.tool_profile == "compact"):
                self.summary_operations.add(operation_id)
                retained = {item["operation_id"] for item in self.registry.list_recent()}
                self.summary_operations.intersection_update(retained)
                self.detail_artifacts = {key: value for key, value in self.detail_artifacts.items() if key in retained}
                self.operation_stages = {key: value for key, value in self.operation_stages.items() if key in retained}
            summary = (self.tool_profile == "compact" or name in NEW_WORKFLOWS
                       or (name == "pro_operation" and operation_id in self.summary_operations))
            if name in {"pro_capabilities", "pro_tool_help", "pro_result", "pro_connection_doctor"}:
                summary = False
            if "result_view" in arguments:
                summary = arguments["result_view"] == "summary"
            include_images = arguments.get("include_images", not summary)
            if name in NEW_WORKFLOWS or (name == "pro_operation" and operation_id in self.summary_operations):
                include_images = arguments.get("include_images", False)
            if name == "pro_result":
                include_images = arguments.get("kind", "details") == "image"
            if summary:
                requested_images = data.get("image_artifacts", []) if include_images else []
                data = self._summary(data)
                if requested_images:
                    data["image_artifacts"] = requested_images
            return self._result(data, include_images=include_images)
        except Exception as exc:
            message = exc.message if isinstance(exc, ValidationError) else str(exc)
            return self._result({"ok": False, "error": message[:1200], "error_type": type(exc).__name__,
                                 "code": getattr(exc, "code", "INVALID_ARGUMENT" if isinstance(exc, ValidationError) else "TOOL_ERROR")})

    def _summary(self, data: dict) -> dict:
        from .results import summarize
        details = None
        if data.get("operation_status") != "running" and data.get("status") != "running":
            identity = data.get("operation_id")
            if identity and identity in self.detail_artifacts:
                details = self.detail_artifacts[identity]
            else:
                try:
                    path = self.store.allocate("-details.json")
                    path.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                    details = (self.store.register(path, "application/json"), str(path))
                    if identity:
                        self.detail_artifacts[identity] = details
                except (OSError, ValueError, TypeError) as exc:
                    data = {**data, "evidence_warnings": [*data.get("evidence_warnings", []),
                        "Detailed evidence could not be published; use pro_result with this operation_id. " + str(exc)[:500]]}
        return summarize(data, details=details)

    def _result(self, data: dict, *, include_images: bool = True) -> types.CallToolResult:
        data = dict(data)
        images = []
        warnings = []
        remaining_bytes = 10 * 1024 * 1024
        # Only artifacts registered by this process can become inline image data.
        for key in data.get("image_artifacts", []) if include_images else []:
            try:
                registered = self.store.items.get(key)
                if registered is not None and registered[0].stat().st_size > remaining_bytes:
                    warnings.append(f"{key}: exceeds the 10 MiB inline image budget; use the local output file.")
                    continue
                raw, mime = self.store.read(key)
                if mime in ("image/png", "image/jpeg"):
                    if len(raw) > remaining_bytes:
                        warnings.append(f"{key}: exceeds the 10 MiB inline image budget; use the local output file.")
                        continue
                    remaining_bytes -= len(raw)
                    images.append(types.ImageContent(type="image", data=base64.b64encode(raw).decode("ascii"), mimeType=mime))
            except (OSError, ValueError, TypeError) as exc:
                warnings.append(f"{key}: image attachment unavailable: {exc}")
        if warnings:
            data["attachment_warnings"] = list(data.get("attachment_warnings", [])) + warnings
        content = [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, allow_nan=False,
                                                                 separators=(",", ":"))), *images]
        return types.CallToolResult(content=content, structuredContent=data,
                                    isError=data.get("ok") is False or data.get("isError") is True)

    async def _dispatch(self, name: str, args: dict) -> dict:
        if not name.startswith("pro_"):
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.call(name, args)))
        if name == "pro_connection_doctor":
            from .connection_doctor import build_connection_report, default_client_config_paths
            return await asyncio.to_thread(build_connection_report,
                tool_profile=self.tool_profile, published_tools=self.tools,
                native_tools=[t for t in self.all_tools.values() if not t.name.startswith("pro_")],
                pro_tools=[t for t in self.all_tools.values() if t.name.startswith("pro_")],
                compact_tools=COMPACT_TOOLS,
                client_configs=default_client_config_paths() if args.get("include_client_configs", False) else ())
        if name == "pro_capabilities":
            return {"ok": True, "version": __version__, "upstream": "solidworks-mcp==0.19.0",
                "native_tool_count": self._native_tool_count, "pro_tool_count": self._pro_tool_count,
                "tool_profile": self.tool_profile, "published_tool_count": len(self.tools),
                "units": {"length": "mm", "angle": "degrees", "mass": "kg"}, "transport": "stdio",
                "workspace": str(self.store.root), "visualize": "Official add-in API, SW2025+; grouping SW2026+",
                "limits": ["Replay protection lasts one server process", "Plans are sequential, not atomic",
                           "PRO lock does not coordinate unrelated MCP servers", "STL units assumed mm"]}
        if name == "pro_operations":
            return {"ok": True, "operations": self.registry.list_recent()}
        if name == "pro_operation":
            result = await self.registry.poll(args["operation_id"], args.get("wait_seconds", 0))
            if args["operation_id"] in self.operation_stages and result.get("operation_status") == "running":
                result["stage"] = self.operation_stages[args["operation_id"]]["value"]
            return result
        if name == "pro_tool_help":
            tool = self.all_tools.get(args["tool"])
            if tool is None:
                raise ValueError("Unknown tool: " + args["tool"])
            return {"ok": True, "tool": tool.model_dump(mode="json", by_alias=True, exclude_none=True),
                    "native_plan_step": not tool.name.startswith("pro_")}
        if name == "pro_result":
            result = await self.registry.poll(args["operation_id"], 0)
            if result.get("operation_status") == "running" or result.get("ok") is False and "operation_id" not in result:
                return result
            if args.get("kind", "details") == "image":
                keys = result.get("image_artifacts", [])
                wanted = args.get("artifact_id")
                if wanted is not None and wanted not in keys:
                    raise ValueError("Image artifact does not belong to this operation")
                if not keys:
                    raise ValueError("This operation has no verified image artifacts")
                key = wanted or keys[-1]
                path, mime = self.store.items[key]
                return {"ok": True, "operation_id": args["operation_id"], "path": str(path),
                        "source_operation_ok": result.get("ok"), "operation_status": result.get("operation_status"),
                        "mime_type": mime, "image_artifacts": [key], "uri": "solidworks-pro://artifact/" + key}
            return result
        if name == "pro_diagnose":
            async def diagnose():
                result = await self.backend.health("diagnose", document_path=args["document_path"], deep=args.get("deep", False))
                result["operations"] = self.registry.list_recent()
                return result
            return await self.registry.run(name, lambda: self._locked(diagnose))
        if name == "pro_repair":
            action = args.get("action", "plan")
            values = {key: value for key, value in args.items() if key not in {"action", "result_view"}}
            key = "repair:" + args["plan_id"] if action == "apply" else None
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.health(action, **values)), key=key)
        if name == "pro_colorize":
            values = {"document_path": args["document_path"], "assignments": args["assignments"],
                      "dry_run": args.get("dry_run", False)}
            key = self._key("colorize", args["request_id"], values)
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.colorize(**values)), key=key)
        if name == "pro_validate_design":
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.design_validate(
                document_path=args["document_path"], assertions=args["assertions"])))
        if name == "pro_variant_study":
            values = {"document_path": args["document_path"], "parameters": args["parameters"],
                      "assertions": args.get("assertions"), "dry_run": args.get("dry_run", True)}
            key = None if values["dry_run"] else self._key("variant_study", args["request_id"], values)
            async def variants():
                result = await self.backend.variant_study(
                    **values, output_directory=str(self.store.allocate("-variants")))
                return self._report_artifacts(result)
            return await self.registry.run(name, lambda: self._locked(variants), wait_seconds=0, key=key)
        if name == "pro_shot_pack":
            from .shot_pack import run_shot_pack
            values = {"document_path": args["document_path"], "shots": args["shots"],
                      "passes": args.get("passes", 100), "denoiser": args.get("denoiser", True),
                      "render_route": args.get("render_route", "addin")}
            key = self._key("shot_pack", args["request_id"], values)
            stage = {"value": "queued"}
            async def shot_pack():
                result = await run_shot_pack(self.backend, self.store, **values,
                                            progress=lambda value: stage.update(value=value))
                return self._report_artifacts(self._job_artifacts(result))
            result = await self.registry.run(name, lambda: self._locked(shot_pack), wait_seconds=0, key=key)
            if result.get("operation_id") and not result.get("replayed"):
                self.operation_stages[result["operation_id"]] = stage
            return result
        if name == "pro_render_job":
            from .render_job import run_render_job
            values = {"document_path": args["document_path"], "mode": args.get("mode", "normal"),
                      "view": args.get("view", "isometric"), "quality": args.get("quality", "standard"),
                      "denoiser": args.get("denoiser", True)}
            values.update(width=args.get("width", 3840 if values["quality"] == "4k" else 1920),
                          height=args.get("height", 2160 if values["quality"] == "4k" else 1080),
                          passes=args.get("passes", 200))
            values.update({key: args[key] for key in ("transforms", "assignments", "width", "height", "passes") if key in args})
            key = self._key("render_job", args["request_id"], values)
            stage = {"value": "queued"}
            async def render_job():
                def progress(value):
                    stage["value"] = value
                result = await run_render_job(self.backend, self.store, **values, progress=progress)
                if result.get("status") == "needs_repair":
                    try:
                        result["repair_plan"] = await self.backend.health("plan", document_path=args["document_path"])
                    except Exception as exc:
                        result["repair_plan"] = {"ok": False, "code": getattr(exc, "code", "REPAIR_PLAN_UNAVAILABLE"),
                                                 "error": str(exc), "mutation_attempted": False}
                return self._job_artifacts(result)
            result = await self.registry.run(name, lambda: self._locked(render_job), wait_seconds=0, key=key)
            if result.get("operation_id") and not result.get("replayed"):
                self.operation_stages[result["operation_id"]] = stage
            return result
        if name == "pro_audit_tail":
            return {"ok": True, "records": self.engine.recent(args.get("limit", 20))}
        if name == "pro_run_plan":
            dry_run = args.get("dry_run", True)
            if dry_run:
                return await self.engine.execute(args["steps"], request_id=args["request_id"], dry_run=True)
            key = self._key("plan", args["request_id"], args)
            async def plan():
                return await self.engine.execute(args["steps"], request_id=args["request_id"], dry_run=False)
            return await self.registry.run(name, plan, key=key)
        if name == "pro_inspect_stl":
            return {"ok": True, **await asyncio.to_thread(inspect_stl, Path(args["path"]))}
        if name == "pro_preview_stl":
            return await asyncio.to_thread(self._preview, Path(args["path"]), args.get("format", "html"), args.get("title", "SolidWorks MCP PRO"))
        if name == "pro_preview_model":
            async def preview():
                stl = self.store.allocate(".stl")
                result = await self.backend.call("export", {"path": str(stl), "quality": "fine"})
                if result.get("ok") is False:
                    return result
                return await asyncio.to_thread(self._preview, stl, "html", args.get("title", "SolidWorks MCP PRO"))
            return await self.registry.run(name, lambda: self._locked(preview))
        if name == "pro_capture_view":
            async def capture():
                path = self.store.allocate(".png")
                result = await self.backend.call("screenshot", {"path": str(path), "view": args.get("view", "iso")})
                if result.get("ok") is False:
                    return result
                uri = self.store.register(path, "image/png")
                return {"ok": True, "path": str(path), "uri": uri, "image_artifacts": [uri.rsplit("/", 1)[1]]}
            return await self.registry.run(name, lambda: self._locked(capture))
        if name == "pro_verify_volume":
            async def verify():
                result = await self.backend.call("get_mass_properties", {})
                if result.get("ok") is False:
                    return result
                actual = float(result["mass_properties"]["volume_mm3"])
                expected = args["expected_mm3"]
                if not math.isfinite(actual):
                    raise ValueError("Measured volume is not finite")
                relative_error = abs(actual - expected) / expected
                tolerance = args.get("relative_tolerance", 0.001)
                return {"ok": relative_error <= tolerance, "passed": relative_error <= tolerance,
                    "actual_mm3": actual, "expected_mm3": expected, "relative_error": relative_error,
                    "relative_tolerance": tolerance}
            return await self.registry.run(name, lambda: self._locked(verify))
        if name == "pro_visualize_status":
            action = "enable" if args.get("load_if_missing", False) else "status"
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.visualize(action)))
        if name == "pro_visualize_render":
            key = self._key("render", args["request_id"], args)
            async def render():
                result = await self.backend.visualize("render", output_dir=self.store.allocate("-render"),
                    width=args.get("width", 1280), height=args.get("height", 720),
                    passes=args.get("passes", 250), denoiser=args.get("denoiser", False),
                    image_format=args.get("image_format", "png"), job_name="render")
                return self._render_artifacts(result)
            return await self.registry.run(name, lambda: self._locked(render), wait_seconds=0, key=key)
        if name == "pro_visualize_export":
            path = self.store.allocate(".svpj")
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.visualize(
                "export_project", path=path, grouping=args.get("grouping", "part"))))
        if name == "pro_visualize_render_project":
            key = self._key("render_project", args["request_id"], args)
            async def render_project():
                values = {k: v for k, v in args.items() if k != "request_id"}
                values["output_directory"] = str(self.store.allocate("-project-render"))
                return self._render_artifacts(await self.backend.visualize("render_project", **values))
            return await self.registry.run(name, lambda: self._locked(render_project), wait_seconds=0, key=key)
        if name == "pro_visualize_open_project":
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.visualize(
                "open_project", path=args["path"])))
        if name == "pro_visualize_appearances":
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.visualize(
                "apply_appearances", document_path=args["document_path"], assignments=args["assignments"],
                dry_run=args.get("dry_run", False))))
        if name == "pro_visualize_camera":
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.visualize(
                "configure_camera", **args)))
        if name == "pro_visualize_named_camera":
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.visualize(
                "named_camera", **args)))
        if name == "pro_visualize_scene":
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.visualize(
                "apply_scene", **args)))
        animation_actions = {"pro_assembly_configurations": "inspect", "pro_assembly_configure": "configure",
                             "pro_assembly_clone": "clone", "pro_assembly_pose": "pose", "pro_assembly_visibility": "visibility",
                             "pro_sheet_bend": "bend"}
        if name in animation_actions:
            return await self.registry.run(name, lambda: self._locked(lambda: self.backend.animation(
                animation_actions[name], **args)))
        raise ValueError("Unimplemented tool: " + name)

    def _render_artifacts(self, result):
        # Bridge reports only verified images. Normalize after its file validation.
        if result.get("ok") is False:
            return result
        keys = []
        for item in result.get("images", []):
            path = Path(item["path"] if isinstance(item, dict) else item)
            # ArtifactStore assigns unique keys even for repeated render.png names.
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            uri = self.store.register(path, mime)
            keys.append(uri.rsplit("/", 1)[1])
        result["image_artifacts"] = keys
        return result

    def _job_artifacts(self, result):
        """Publish already verified preview evidence even if the final failed."""
        keys = []
        for field, target in (("preview_images", "preview_artifacts"), ("images", "final_artifacts")):
            result[target] = []
            for item in result.get(field, []):
                try:
                    path = Path(item["path"])
                    uri = self.store.register(path, "image/png")
                    result[target].append(uri.rsplit("/", 1)[1])
                except (OSError, ValueError, TypeError) as exc:
                    result.setdefault("artifact_warnings", []).append(
                        f"Verified image could not be published; its local path remains in full results: {str(exc)[:500]}")
            keys.extend(result[target])
        result["image_artifacts"] = keys
        return result

    def _report_artifacts(self, result):
        """Publish only existing workflow reports inside the owned workspace."""
        for source, destination, mime in (("csv_path", "csv_uri", "text/csv"),
                                          ("manifest_path", "manifest_uri", "application/json")):
            path = result.get(source)
            if not path and source == "manifest_path" and isinstance(result.get("manifest"), dict):
                path = result["manifest"].get("path")
            if path:
                try:
                    result[destination] = self.store.register(Path(path), mime)
                except (OSError, ValueError, TypeError) as exc:
                    result.setdefault("artifact_warnings", []).append(
                        f"Report remains available by local path; publication failed: {str(exc)[:500]}")
        return result

    def _preview(self, path: Path, format: str, title: str) -> dict:
        output = self.store.allocate(".html" if format == "html" else ".svg")
        result = (build_viewer if format == "html" else render_svg)(path, output, title=title)
        uri = self.store.register(output, "text/html" if format == "html" else "image/svg+xml")
        return {"ok": True, **result, "resource_uri": uri}


async def serve(workspace: Path, *, tool_profile: str = "full"):
    backend = NativeBackend()
    app = ProApplication(backend, workspace, tool_profile=tool_profile)
    try:
        await app.initialize()
        async with stdio_server() as (read, write):
            await app.server.run(read, write, app.server.create_initialization_options())
    finally:
        await app.registry.drain()
        backend.close()


def doctor() -> dict:
    versions = {}
    for package in ("solidworks-mcp-pro", "solidworks-mcp", "mcp", "pywin32", "comtypes", "Pillow"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return {"python": platform.python_version(), "platform": platform.platform(), "packages": versions,
        "workspace": str(default_workspace()), "note": "Dependency check only; use get_status and pro_visualize_status for live availability."}


def main():
    parser = argparse.ArgumentParser(description="SolidWorks MCP PRO — Codex/Claude stdio server")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--doctor", action="store_true", help="print dependency report and exit without CAD")
    parser.add_argument("--connection-doctor", action="store_true", help="print local identity/profile report without CAD; use pro_connection_doctor for the live catalogue")
    parser.add_argument("--workspace", type=Path, default=default_workspace())
    parser.add_argument("--tool-profile", choices=("compact", "full"), default="full",
                        help="compact exposes workflows and on-demand tool help; full preserves all tools")
    args = parser.parse_args()
    if args.doctor:
        print(json.dumps(doctor(), indent=2))
    elif args.connection_doctor:
        from .connection_doctor import main as connection_doctor_main
        raise SystemExit(connection_doctor_main(["--tool-profile", args.tool_profile]))
    else:
        asyncio.run(serve(args.workspace, tool_profile=args.tool_profile))


if __name__ == "__main__":
    main()
