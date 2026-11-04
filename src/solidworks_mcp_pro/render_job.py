"""One server-owned, sequential static render workflow.

The application owns the operation registry and CAD lease. Native work is
drained even on task cancellation, and temporary poses/views are restored
before the caller can release that lease. Requested RGB changes remain unsaved.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import math
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

from .camera_bridge import STANDARD_VIEWS, _document_file
from .pose_bridge import _vector
from .visualize_bridge import _verify_images


class RenderJobError(RuntimeError):
    def __init__(self, code: str, message: str, result: dict | None = None):
        super().__init__(message)
        self.code, self.result = code, result


async def _drain(awaitable: Awaitable, *, cleanup: bool = False) -> tuple[Any, bool]:
    """Keep native-task ownership until completion, including repeated cancels."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if task.done():
                result = task.result()
                break
        except Exception:
            if cancelled and not cleanup:
                raise asyncio.CancelledError() from None
            raise
    if cancelled and not cleanup:
        raise asyncio.CancelledError()
    return result, cancelled


def _validate(document_path, mode, transforms, assignments, view, quality, width, height, passes, denoiser):
    path = _document_file(document_path)
    if mode not in {"normal", "exploded"} or quality not in {"standard", "4k"}:
        raise ValueError("mode must be normal/exploded and quality must be standard/4k.")
    if view not in STANDARD_VIEWS and view != "current":
        raise ValueError("view must name a standard model view or current.")
    if type(denoiser) is not bool:
        raise ValueError("denoiser must be boolean.")
    settings = {"width": 3840 if quality == "4k" else 1920,
                "height": 2160 if quality == "4k" else 1080, "passes": 200, "denoiser": denoiser}
    for key, value, high in (("width", width, 4096), ("height", height, 4096), ("passes", passes, 5000)):
        if value is not None:
            if type(value) is not int or not (1 if key == "passes" else 64) <= value <= high:
                raise ValueError(f"Invalid {key} override.")
            settings[key] = value
    poses = deepcopy([] if transforms is None else transforms)
    if not isinstance(poses, list) or len(poses) > 128:
        raise ValueError("transforms must contain at most 128 explicit poses.")
    if mode == "normal" and poses:
        raise ValueError("transforms are only accepted in exploded mode.")
    if mode == "exploded" and (not poses or path.suffix.lower() != ".sldasm"):
        raise ValueError("Exploded mode requires an assembly and 1..128 explicit poses.")
    names = set()
    for item in poses:
        if not isinstance(item, dict) or set(item) != {"component", "position_mm", "rotation_deg"}:
            raise ValueError("Each pose requires component, position_mm and rotation_deg.")
        name = item["component"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 512 or name in names
                or any(ord(c) < 32 for c in name) or "/" in name or "\\" in name):
            raise ValueError("Poses require unique exact top-level component names.")
        names.add(name)
        _vector(item["position_mm"], "position_mm", 1e6)
        _vector(item["rotation_deg"], "rotation_deg", 3600)
    colors = deepcopy([] if assignments is None else assignments)
    if not isinstance(colors, list) or len(colors) > 128:
        raise ValueError("assignments must contain at most 128 RGB assignments.")
    for item in colors:
        if not isinstance(item, dict) or set(item) - {"component", "color_rgb"} or "color_rgb" not in item:
            raise ValueError("Each assignment requires color_rgb and optionally component.")
        rgb = item["color_rgb"]
        if (not isinstance(rgb, list) or len(rgb) != 3
                or any(type(v) is not int or not 0 <= v <= 255 for v in rgb)):
            raise ValueError("color_rgb must contain three integers in [0,255].")
    return str(path), poses, colors, settings


async def run_render_job(
    backend, store, *, document_path: str | Path, mode: str = "normal",
    transforms: list[dict] | None = None, assignments: list[dict] | None = None,
    view: str = "isometric", quality: str = "standard", width: int | None = None,
    height: int | None = None, passes: int | None = None, denoiser: bool = True,
    progress: Callable[[str], Any] | None = None,
) -> dict:
    """Preflight once, color/pose/frame, preview, final, then exact cleanup.

    The preview gate verifies image integrity and requested dimensions, not
    aesthetic quality. No automatic geometry repair or implicit disassembly is
    attempted. Caller must hold its CAD lease throughout this coroutine.
    """
    started = time.perf_counter()
    stages, cleanup_results, warnings = [], {}, []
    result = {"ok": False, "status": "failed", "saved": False, "rebuilt_after_pose": False,
              "mode": mode, "stages": stages, "cleanup": cleanup_results,
              "visual_review_required": True}
    camera_snapshot = None
    pose_snapshot = []
    pose_attempted = False
    cancelled = False

    async def stage(name, awaitable, *, restoring=False, allow_failure=False):
        nonlocal cancelled
        entry = {"stage": name, "status": "running"}
        stages.append(entry)
        start = time.perf_counter()
        native_started = False
        try:
            if progress is not None and not restoring:
                try:
                    notification = progress(name)
                    if inspect.isawaitable(notification):
                        await notification
                except Exception as exc:
                    warnings.append(f"Progress notification failed: {exc}")
            native_started = True
            value, was_cancelled = await _drain(awaitable, cleanup=restoring)
            cancelled = cancelled or was_cancelled
            entry.update(status="completed", result=value)
            if not isinstance(value, dict) or value.get("ok") is not True:
                entry["status"] = "failed"
                if not allow_failure or not isinstance(value, dict):
                    raise RenderJobError(value.get("code", "RENDER_JOB_STAGE_FAILED") if isinstance(value, dict)
                                         else "RENDER_JOB_STAGE_FAILED", f"Render job stage {name} failed.", value)
            return value
        except asyncio.CancelledError:
            if not native_started:
                if inspect.iscoroutine(awaitable):
                    awaitable.close()
                else:
                    await _drain(awaitable, cleanup=True)
            entry["status"] = "cancelled"
            raise
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
            raise
        finally:
            entry["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)

    async def render(name, settings):
        directory = store.allocate(f"-{name}")
        async def perform():
            value = await backend.visualize("render", output_dir=directory,
                document_path=document_path, **settings, image_format="png", job_name=name)
            if isinstance(value, dict) and value.get("ok") is True:
                # A successful dispatch alone cannot open the final-render gate.
                value["images"] = _verify_images(directory, "png", settings["width"], settings["height"])
            return value
        return await stage(name, perform())

    try:
        document_path, poses, colors, final_settings = _validate(
            document_path, mode, transforms, assignments, view, quality, width, height, passes, denoiser)
        result["document_path"] = document_path
        preview_settings = {"width": 960, "height": 540, "passes": 50, "denoiser": denoiser}
        result["settings"] = {"preview": preview_settings, "final": final_settings}
        diagnostic = await stage("diagnose", backend.health("diagnose", document_path=document_path, deep=False),
                                 allow_failure=True)
        result["diagnosis"] = diagnostic
        if (diagnostic.get("ok") is not True or diagnostic.get("healthy") is not True
                or diagnostic.get("errors") or diagnostic.get("uninspected")):
            result.update(status="needs_repair", code="MODEL_NEEDS_REPAIR")
        else:
            await stage("renderer_preflight", backend.visualize("status"))
            if colors:
                await stage("color_preflight", backend.colorize(
                    document_path=document_path, assignments=colors, dry_run=True))
            if poses:
                inventory = await stage("pose_preflight", backend.animation(
                    "pose", document_path=document_path, transforms=poses, dry_run=True, _exact_active=True))
                pose_snapshot = inventory.get("components", [])
                expected = {item["component"] for item in poses}
                if (inventory.get("read_only") or len(pose_snapshot) != len(expected)
                        or {item.get("component") for item in pose_snapshot} != expected
                        or any(item.get("suppressed") or not item.get("rigid_unit_scale")
                               or not isinstance(item.get("transform"), list) or len(item["transform"]) != 16
                               for item in pose_snapshot)):
                    raise RenderJobError("POSE_PREFLIGHT_FAILED", "Every target needs a writable rigid native matrix.")
                if any(any(type(v) not in (int, float) or not math.isfinite(v) for v in item["transform"])
                       for item in pose_snapshot):
                    raise RenderJobError("POSE_PREFLIGHT_FAILED", "Captured native matrices must be finite.")
            captured = await stage("camera_capture", backend.camera_state("capture", document_path=document_path))
            camera_snapshot = captured["state"]
            if colors:
                color_result = await stage("colorize", backend.colorize(
                    document_path=document_path, assignments=colors, dry_run=False))
                result["color_changes"] = color_result
                result["changes_may_remain"] = color_result.get("mutation_attempted", True)
            if poses:
                pose_attempted = True
                await stage("pose", backend.animation("pose", document_path=document_path,
                                                       transforms=poses, dry_run=False, _exact_active=True))
            await stage("camera", backend.visualize("configure_camera", document_path=document_path,
                                                    action="apply", view=view, fit=True, _exact_active=True))
            preview = await render("preview", preview_settings)
            result["preview_images"] = preview["images"]
            final = await render("final", final_settings)
            result.update(ok=True, status="completed", images=final["images"],
                          verification="Native decoded PNG dimensions and hashes; visual quality requires review.")
    except asyncio.CancelledError:
        cancelled = True
        result.update(status="indeterminate", code="RENDER_JOB_CANCELLED")
    except Exception as exc:
        result.update(ok=False, status="failed", code=getattr(exc, "code", "RENDER_JOB_FAILED"), error=str(exc),
                      failed_stage=stages[-1]["stage"] if stages else "validate")
        if isinstance(exc, RenderJobError) and exc.result is not None:
            result["failure"] = exc.result
            if isinstance(exc.result, dict):
                for key in ("rollback", "partial_state_possible", "outcome_may_be_indeterminate", "mutation_attempted"):
                    if key in exc.result:
                        result[key] = deepcopy(exc.result[key])
                rollback = exc.result.get("rollback")
                if (exc.result.get("partial_state_possible") or exc.result.get("outcome_may_be_indeterminate")
                        or isinstance(rollback, dict) and rollback.get("verified") is False):
                    result.update(status="indeterminate", outcome_may_be_indeterminate=True)
        if result["code"] == "RENDER_SETTINGS_RESTORE_FAILED":
            result.update(status="indeterminate", outcome_may_be_indeterminate=True)
    finally:
        # Restore component positions before the original camera framing. Even
        # failed native calls can have changed state before reporting failure.
        for name, needed, operation in (
            ("poses", pose_attempted, lambda: backend.pose_state(
                "restore", document_path=document_path, states=pose_snapshot)),
            ("camera", camera_snapshot is not None, lambda: backend.camera_state(
                "restore", document_path=document_path, state=camera_snapshot)),
        ):
            if not needed:
                continue
            try:
                restored = await stage(f"restore_{name}", operation(), restoring=True)
                cleanup_results[name] = restored
                if restored.get("verified") is not True:
                    raise RenderJobError("RENDER_STATE_RESTORE_FAILED", f"{name} restoration is unverified.")
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                cancelled = cancelled or isinstance(exc, asyncio.CancelledError)
                failure = exc.result if isinstance(exc, RenderJobError) and isinstance(exc.result, dict) else None
                cleanup_results.setdefault(name, failure or {"ok": False, "verified": False, "error": str(exc)})
                cleanup_results[name]["verified"] = False
        failed_cleanup = any(item.get("verified") is not True for item in cleanup_results.values())
        if failed_cleanup:
            result.update(ok=False, status="indeterminate", code="RENDER_STATE_RESTORE_FAILED",
                          outcome_may_be_indeterminate=True)
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if warnings:
            result["warnings"] = warnings
    if cancelled:
        raise asyncio.CancelledError()
    return result
