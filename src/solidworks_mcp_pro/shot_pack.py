"""Sequential native product shots with one camera checkpoint and verified PNGs.

The server owns the operation registry and CAD lease. This module does not save,
rebuild, recolor or pose a model. A preview checks integrity, not artistic quality;
the original camera is restored and read back before returning any outcome.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable

from mcp import types

from .camera_bridge import VIEWS, _document_file, _number, view_rotation
from .fast_tools import REQUEST, STRING, VIEW, object_schema
from .render_job import _drain
from .visualize_bridge import _verify_images


ASPECTS = {"square": (1024, 1024), "landscape": (1920, 1080), "portrait": (1080, 1920)}
SHOT_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"
SHOT_FIELDS = {"name", "view", "aspect", "width", "height", "from_direction", "up_direction", "zoom_factor"}
PROJECT_DIRECTIONS = {"front": [0, 0, 1], "back": [0, 0, -1], "left": [-1, 0, 0],
                      "right": [1, 0, 0], "isometric": [1, 1, 1]}
PROJECT_VIEWS = set(PROJECT_DIRECTIONS) | {"iso", "custom"}


class ShotPackError(RuntimeError):
    def __init__(self, code: str, message: str, result: dict | None = None):
        self.code, self.result = code, result
        super().__init__(message)


def shot_tools() -> list[types.Tool]:
    vector = {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number"}}
    shot = object_schema({
        "name": {"type": "string", "pattern": SHOT_NAME},
        "view": {"type": "string", "enum": sorted(VIEWS | {"iso"})},
        "aspect": {"type": "string", "enum": list(ASPECTS), "default": "landscape"},
        "width": {"type": "integer", "minimum": 64, "maximum": 4096,
                  "description": "Supply width and height together to override the preset dimensions."},
        "height": {"type": "integer", "minimum": 64, "maximum": 4096},
        "from_direction": vector, "up_direction": vector,
        "zoom_factor": {"type": "number", "minimum": 0.25, "maximum": 2, "default": 1},
    }, ("name", "view"))
    shot["dependentRequired"] = {"width": ["height"], "height": ["width"]}
    shot["allOf"] = [{
        "if": {"properties": {"view": {"const": "custom"}}, "required": ["view"]},
        "then": {"required": ["from_direction"]},
        "else": {"not": {"anyOf": [{"required": ["from_direction"]}, {"required": ["up_direction"]}]}},
    }]
    schema = object_schema({"document_path": STRING, "request_id": REQUEST,
            "shots": {"type": "array", "minItems": 1, "maxItems": 12, "items": shot},
            "render_route": {"type": "string", "enum": ["addin", "project"], "default": "addin",
                "description": "Explicit route; never falls back. Project exports once and uses a bounding-sphere-fit native project camera. Project supports front/back/left/right/isometric or world-Y-up custom views and zoom_factor<=1."},
            "passes": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 100},
            "denoiser": {"type": "boolean", "default": True}, "result_view": VIEW},
            ("document_path", "request_id", "shots"))
    schema["allOf"] = [{
        "if": {"properties": {"render_route": {"const": "project"}}, "required": ["render_route"]},
        "then": {"properties": {"shots": {"items": {"properties": {
            "view": {"enum": sorted(PROJECT_VIEWS)}, "zoom_factor": {"maximum": 1},
            "up_direction": {"const": [0, 1, 0]},
        }}}}},
    }]
    return [types.Tool(name="pro_shot_pack",
        description="Create 1-12 explicit native product shots with aspect-matched previews, final PNGs, a contact sheet and JSON manifest. Exact active saved part/assembly only. Opt-in project route uses explicit fit cameras; default addin route uses native model-view transfer. Restores the original model-view camera; no CAD save/rebuild or automatic explosion. Returns an operation_id; poll it instead of resubmitting.",
        inputSchema=schema,
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=False, openWorldHint=False))]


def preview_dimensions(width: int, height: int, maximum_edge: int = 640) -> tuple[int, int]:
    """Keep the exact rational aspect and the renderer's 64px minimum.

    Coprime or very narrow dimensions can require a larger preview. This is an
    integer-pixel limitation, so such cases use fewer passes at the exact ratio
    rather than silently changing composition or promising a small raster.
    """
    divisor = math.gcd(width, height)
    x, y = width // divisor, height // divisor
    minimum_factor = max(math.ceil(64 / x), math.ceil(64 / y))
    factor = min(divisor, max(minimum_factor, maximum_edge // max(x, y)))
    return x * factor, y * factor


def _validate(document_path, shots, passes, denoiser, render_route):
    path = _document_file(document_path)
    if not isinstance(render_route, str) or render_route not in {"addin", "project"}:
        raise ValueError("render_route must be addin or project; automatic fallback is not supported")
    if type(passes) is not int or not 1 <= passes <= 5000 or type(denoiser) is not bool:
        raise ValueError("passes must be an integer in 1..5000 and denoiser must be boolean")
    if not isinstance(shots, list) or not 1 <= len(shots) <= 12:
        raise ValueError("shots must contain 1..12 explicit shots")
    normalized, names = [], set()
    for shot in deepcopy(shots):
        if not isinstance(shot, dict) or set(shot) - SHOT_FIELDS:
            raise ValueError("Each shot must be an object with only documented fields")
        name, view, aspect = shot.get("name"), shot.get("view"), shot.get("aspect", "landscape")
        if not isinstance(name, str) or re.fullmatch(SHOT_NAME, name) is None or name.casefold() in names:
            raise ValueError("Shot names must be unique (case-insensitive), 1..40 ASCII letters/digits/_/-")
        names.add(name.casefold())
        if not isinstance(view, str) or view not in VIEWS | {"iso"}:
            raise ValueError("Each shot requires a standard view, iso, current or custom")
        if not isinstance(aspect, str) or aspect not in ASPECTS:
            raise ValueError("aspect must be square, landscape or portrait")
        if ("width" in shot) != ("height" in shot):
            raise ValueError("width and height overrides must be supplied together")
        width, height = shot.get("width", ASPECTS[aspect][0]), shot.get("height", ASPECTS[aspect][1])
        for dimension in (width, height):
            if type(dimension) is not int or not 64 <= dimension <= 4096:
                raise ValueError("Final dimensions must be integers in 64..4096")
        zoom = _number(shot.get("zoom_factor", 1), "zoom_factor", .25, 2)
        camera = {"view": "isometric" if view == "iso" else view, "fit": True, "zoom_factor": zoom}
        if view == "custom":
            # Validate every vector before any shot or native state can change.
            view_rotation(shot.get("from_direction"), shot.get("up_direction"))
            camera["from_direction"] = list(shot["from_direction"])
            if shot.get("up_direction") is not None:
                camera["up_direction"] = list(shot["up_direction"])
        elif "from_direction" in shot or "up_direction" in shot:
            raise ValueError("Direction vectors are accepted only for custom shots")
        if render_route == "project":
            if view not in PROJECT_VIEWS:
                raise ValueError("Project rendering supports front/back/left/right/isometric or world-Y-up custom views")
            if "up_direction" in shot and shot["up_direction"] != [0, 1, 0]:
                raise ValueError("Project rendering currently supports only world-Y-up custom cameras")
            if zoom > 1:
                raise ValueError("Project fit cameras require zoom_factor<=1; intentional close-up cropping is unsupported")
        preview_width, preview_height = preview_dimensions(width, height)
        normalized.append({"name": name, "aspect": aspect, "camera": camera, "status": "not_started",
            "preview_settings": {"width": preview_width, "height": preview_height,
                                 "passes": min(25, passes), "denoiser": denoiser},
            "final_settings": {"width": width, "height": height, "passes": passes, "denoiser": denoiser}})
    return str(path), normalized


def project_fit_camera(bounds: dict, camera: dict, width: int, height: int) -> dict:
    """Fit the complete axis-aligned bounding sphere in the narrower 32deg frustum.

    A 10% distance margin protects all box corners in both landscape and portrait.
    zoom_factor<=1 only moves farther away. The returned native project camera is
    independent of SOLIDWORKS' model-view scale or export auto-fit behavior.
    """
    if not isinstance(bounds, dict):
        raise ShotPackError("PROJECT_BOUNDS_UNAVAILABLE", "Native model bounds are required for project framing")
    low, high = bounds.get("min_mm"), bounds.get("max_mm")
    for values in (low, high):
        if (not isinstance(values, (list, tuple)) or len(values) != 3
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
            raise ShotPackError("PROJECT_BOUNDS_INVALID", "Native model bounds must contain finite millimetre coordinates")
    sizes = [b-a for a, b in zip(low, high)]
    if any(size < 0 or not math.isfinite(size) for size in sizes):
        raise ShotPackError("PROJECT_BOUNDS_INVALID", "Native model bounding-box extents are invalid")
    radius = math.hypot(*sizes) / 2
    if not math.isfinite(radius) or radius <= 0:
        raise ShotPackError("PROJECT_BOUNDS_INVALID", "A positive finite model bounding sphere is required")
    center = [a+size/2 for a, size in zip(low, sizes)]
    direction = camera.get("from_direction") if camera["view"] == "custom" else PROJECT_DIRECTIONS[camera["view"]]
    rotation = view_rotation(direction, [0, 1, 0])
    toward_camera = [rotation[2], rotation[5], rotation[8]]
    vertical_half_angle = math.radians(32) / 2
    horizontal_half_angle = math.atan(math.tan(vertical_half_angle) * width / height)
    distance = radius / math.sin(min(vertical_half_angle, horizontal_half_angle)) * 1.1 / camera["zoom_factor"]
    position = [value+axis*distance for value, axis in zip(center, toward_camera)]
    if any(not math.isfinite(value) or abs(value) > 1e6 for value in [*center, *position]):
        raise ShotPackError("PROJECT_CAMERA_OUT_OF_RANGE", "Fit camera exceeds supported native project coordinates")
    return {"camera_position_mm": position, "camera_target_mm": center, "vertical_fov_deg": 32}


def _file(path: Path, **extra) -> dict:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest, **extra}


def _contact_sheet(shots: list[dict], path: Path, *, complete: bool) -> dict | None:
    from PIL import Image, ImageDraw, ImageOps

    completed = [shot for shot in shots if shot.get("images")]
    if not completed:
        return None
    columns = min(3, len(completed))
    rows = math.ceil(len(completed) / columns)
    tile_width, tile_height, label_height, margin, heading = 320, 240, 36, 16, 44
    size = (columns * (tile_width + margin) + margin,
            rows * (tile_height + label_height + margin) + margin + heading)
    sheet = Image.new("RGB", size, (238, 240, 243))
    draw = ImageDraw.Draw(sheet)
    draw.text((margin, 15), "Verified native shots" if complete else "Partial native shot outputs", fill=(20, 25, 30))
    for index, shot in enumerate(completed):
        image_path = Path(shot["images"][0]["path"])
        with Image.open(image_path) as image:
            image.load()
            thumbnail = ImageOps.contain(image.convert("RGB"), (tile_width, tile_height), Image.Resampling.LANCZOS)
        left = margin + (index % columns) * (tile_width + margin)
        top = heading + margin + (index // columns) * (tile_height + label_height + margin)
        draw.rectangle((left, top, left + tile_width - 1, top + tile_height - 1), fill="white")
        sheet.paste(thumbnail, (left + (tile_width-thumbnail.width)//2, top + (tile_height-thumbnail.height)//2))
        settings = shot["final_settings"]
        label = f"{shot['name']} | {settings['width']}x{settings['height']}"
        draw.text((left, top + tile_height + 9), label, fill=(20, 25, 30))
    sheet.save(path, "PNG")
    with Image.open(path) as image:
        image.verify()
    return _file(path, width=size[0], height=size[1], mime_type="image/png", role="contact_sheet")


async def run_shot_pack(
    backend, store, *, document_path: str | Path, shots: list[dict], passes: int = 100,
    denoiser: bool = True, render_route: str = "addin", progress: Callable[[str], Any] | None = None,
) -> dict:
    """Render one exact saved active model; caller owns registry/replay/CAD lease."""
    started = time.perf_counter()
    result = {"ok": False, "status": "failed", "mode": "normal", "saved": False, "rebuilt": False,
              "render_route": render_route,
              "visual_review_required": True, "stages": [], "shots": [], "images": [],
              "preview_images": [], "cleanup": {}}
    camera_snapshot = None
    directory = None
    directory_created = False
    cancelled = False
    warnings = []
    active_shot = None
    exported_project = None

    async def stage(name, awaitable, *, restoring=False, allow_failure=False):
        nonlocal cancelled
        record = {"stage": name, "status": "running"}
        result["stages"].append(record)
        before = time.perf_counter()
        native_started = False
        try:
            if progress is not None and not restoring:
                try:
                    notification = progress(name)
                    if inspect.isawaitable(notification):
                        await notification
                except Exception as exc:
                    warnings.append(f"Progress notification failed: {str(exc)[:300]}")
            native_started = True
            value, interrupted = await _drain(awaitable, cleanup=restoring)
            cancelled = cancelled or interrupted
            record.update(status="completed", result=value)
            if not isinstance(value, dict) or value.get("ok") is not True:
                record["status"] = "failed"
                if not allow_failure or not isinstance(value, dict):
                    raise ShotPackError(value.get("code", "SHOT_STAGE_FAILED") if isinstance(value, dict)
                                        else "SHOT_STAGE_FAILED", f"Shot pack stage {name} failed", value)
            return value
        except asyncio.CancelledError:
            if not native_started:
                if inspect.iscoroutine(awaitable):
                    awaitable.close()
                else:
                    await _drain(awaitable, cleanup=True)
            record["status"] = "cancelled"
            raise
        except Exception as exc:
            record.update(status="failed", error=str(exc))
            raise
        finally:
            record["duration_ms"] = round((time.perf_counter() - before) * 1000, 3)

    async def render(shot, prefix, phase):
        settings = shot[f"{phase}_settings"]
        folder = directory / f"{prefix}-{phase}"

        async def perform():
            if render_route == "project":
                value = await backend.visualize("render_project", project_path=str(exported_project),
                    output_directory=folder, **settings, **shot["project_camera"])
                execution = "native_offline_renderer_project_copy"
                if (isinstance(value, dict) and value.get("ok") is True
                        and (not isinstance(value.get("renderer"), dict)
                             or value["renderer"].get("native_execution") is not True)):
                    raise ShotPackError("RENDER_ROUTE_UNVERIFIED", "Native project renderer execution was not reported")
            else:
                value = await backend.visualize("render", document_path=document_path, output_dir=folder,
                    **settings, image_format="png", job_name=f"{prefix}-{phase}")
                execution = value.get("execution", "addin_execution_not_reported") if isinstance(value, dict) else None
            if isinstance(value, dict) and value.get("ok") is True:
                images = _verify_images(folder, "png", settings["width"], settings["height"])
                if len(images) != 1:
                    raise ShotPackError("SHOT_OUTPUT_AMBIGUOUS", "Each shot must produce exactly one PNG")
                value.update(render_route=render_route, execution=execution)
                value["images"] = [{**images[0], "shot": shot["name"], "role": phase,
                                    "render_route": render_route, "execution": execution}]
            return value
        return await stage(f"{prefix}_{phase}", perform())

    try:
        document_path, normalized = _validate(document_path, shots, passes, denoiser, render_route)
        result.update(document_path=document_path, shots=normalized)
        # Allocate one new owned output directory only after all inputs pass.
        directory = store.allocate("-shot-pack")
        if not directory.resolve().is_relative_to(store.root.resolve()):
            raise ShotPackError("INVALID_OUTPUT_PATH", "Shot pack output must remain inside the artifact workspace")
        directory.mkdir(exist_ok=False)
        directory_created = True
        result["output_directory"] = str(directory)
        diagnosis = await stage("diagnose", backend.health("diagnose", document_path=document_path, deep=False),
                                allow_failure=True)
        result["diagnosis"] = diagnosis
        if (diagnosis.get("ok") is not True or diagnosis.get("healthy") is not True
                or diagnosis.get("errors") or diagnosis.get("uninspected")):
            result.update(status="needs_repair", code="MODEL_NEEDS_REPAIR")
        else:
            await stage("renderer_preflight", backend.visualize("status"))
            captured = await stage("camera_capture", backend.camera_state("capture", document_path=document_path))
            if not isinstance(captured.get("state"), dict):
                raise ShotPackError("CAMERA_CAPTURE_INVALID", "A native camera state checkpoint is required")
            camera_snapshot = deepcopy(captured["state"])
            if render_route == "project":
                bounds_report = await stage("project_bounds", backend.design_validate(
                    document_path=document_path, assertions=[{"id": f"bbox-{axis}", "kind": "bbox_size",
                        "axis": axis, "unit": "mm", "min": 0, "abs_tolerance": 0} for axis in "xyz"]))
                if bounds_report.get("passed") is not True or bounds_report.get("state_unchanged") is not True:
                    raise ShotPackError("PROJECT_BOUNDS_UNVERIFIED", "Native bounds require completed, unchanged-state checks",
                                        bounds_report)
                bounds = bounds_report.get("measurements", {}).get("bounding_box_mm")
                result["bounding_box_mm"] = deepcopy(bounds)
                for shot in normalized:
                    settings = shot["final_settings"]
                    shot["project_camera"] = project_fit_camera(bounds, shot["camera"],
                                                               settings["width"], settings["height"])
                exported_project = directory / "source.svpj"
                exported = await stage("project_export", backend.visualize("export_project",
                    document_path=document_path, path=exported_project, grouping="part"))
                if not exported_project.is_file() or exported_project.is_symlink() or not exported_project.stat().st_size:
                    raise ShotPackError("PROJECT_OUTPUT_MISSING", "Native export did not produce an owned SVPJ")
                result["exported_project"] = {**_file(exported_project), "native_export": exported}
            for index, shot in enumerate(normalized):
                active_shot = shot
                shot["status"] = "running"
                prefix = f"s{index+1:02d}-{shot['name']}"
                if render_route == "addin" and shot["camera"]["view"] == "current" and index:
                    reset = await stage(f"{prefix}_original_view", backend.camera_state("restore",
                        document_path=document_path, state=camera_snapshot))
                    if reset.get("verified") is not True:
                        raise ShotPackError("RENDER_STATE_RESTORE_FAILED", "Original-view shot reset is unverified", reset)
                if render_route == "addin":
                    shot["camera_result"] = await stage(f"{prefix}_camera", backend.visualize("configure_camera",
                        document_path=document_path, action="apply", _exact_active=True, **shot["camera"]))
                preview = await render(shot, prefix, "preview")
                shot["preview_images"] = preview["images"]
                result["preview_images"].extend(preview["images"])
                final = await render(shot, prefix, "final")
                shot["images"] = final["images"]
                result["images"].extend(final["images"])
                shot["status"] = "completed"
                active_shot = None
            result.update(ok=True, status="completed",
                verification="Native PNGs fully decoded with exact requested dimensions and SHA-256; visual review required.")
    except asyncio.CancelledError:
        cancelled = True
        result.update(ok=False, status="indeterminate", code="SHOT_PACK_CANCELLED", outcome_may_be_indeterminate=True)
        if active_shot is not None:
            active_shot["status"] = "indeterminate"
    except Exception as exc:
        result.update(ok=False, status="failed", code=getattr(exc, "code", "SHOT_PACK_FAILED"), error=str(exc),
                      failed_stage=result["stages"][-1]["stage"] if result["stages"] else "validate")
        if active_shot is not None:
            active_shot["status"] = "failed"
        if isinstance(exc, ShotPackError) and exc.result is not None:
            result["failure"] = exc.result
        failure = result.get("failure")
        if (result["code"] in {"RENDER_SETTINGS_RESTORE_FAILED", "RENDER_STATE_RESTORE_FAILED"}
                or isinstance(failure, dict) and (failure.get("outcome_may_be_indeterminate")
                    or failure.get("partial_state_possible"))):
            result.update(status="indeterminate", outcome_may_be_indeterminate=True)
    finally:
        if camera_snapshot is not None:
            try:
                restored = await stage("restore_camera", backend.camera_state("restore",
                    document_path=document_path, state=camera_snapshot), restoring=True)
                result["cleanup"]["camera"] = restored
                if restored.get("verified") is not True:
                    raise ShotPackError("RENDER_STATE_RESTORE_FAILED", "Original camera restoration is unverified", restored)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                cancelled = cancelled or isinstance(exc, asyncio.CancelledError)
                result["cleanup"]["camera"] = {"ok": False, "verified": False, "error": str(exc)}
                if isinstance(exc, ShotPackError) and isinstance(exc.result, dict):
                    result["cleanup"]["camera"].update(exc.result, verified=False)
                result.update(ok=False, status="indeterminate", code="RENDER_STATE_RESTORE_FAILED",
                              outcome_may_be_indeterminate=True)
        if cancelled:
            result.update(ok=False, status="indeterminate", code="SHOT_PACK_CANCELLED",
                          outcome_may_be_indeterminate=True)
        result["counts"] = {"requested_shots": len(result["shots"]),
                            "completed_shots": sum(shot["status"] == "completed" for shot in result["shots"]),
                            "verified_previews": len(result["preview_images"]), "verified_finals": len(result["images"])}
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if warnings:
            result["warnings"] = warnings
        if directory_created:
            def artifact_error(exc):
                result.setdefault("artifact_warnings", []).append(str(exc)[:500])
                if result["ok"]:
                    result.update(ok=False, status="failed", code="SHOT_ARTIFACT_FAILED", error=str(exc))

            try:
                contact = _contact_sheet(result["shots"], directory / "contact-sheet.png", complete=result["ok"])
                if contact is not None:
                    result["contact_sheet"] = contact
                    result["images"].append(contact)
            except Exception as exc:
                artifact_error(exc)
            result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            manifest = directory / "manifest.json"
            result["manifest_path"] = str(manifest)
            manifest_created = False
            try:
                # Only this workflow's own manifest can ever be rewritten.
                with manifest.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=True, allow_nan=False, indent=2)
                manifest_created = True
                manifest_uri = store.register(manifest, "application/json")
                result["manifest"] = _file(manifest, mime_type="application/json", uri=manifest_uri)
            except Exception as exc:
                artifact_error(exc)
                if manifest_created:
                    try:
                        manifest.write_text(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2),
                                            encoding="utf-8")
                        result["manifest"] = _file(manifest, mime_type="application/json")
                    except Exception as rewrite_error:
                        artifact_error(rewrite_error)
    if cancelled:
        raise asyncio.CancelledError()
    return result
