"""Opt-in render acceptance with scratch ownership and route-specific evidence.

The add-in route requires an empty running SOLIDWORKS session. It creates and
closes only its own part. The project route renders an owned copy of an existing
SVPJ without attaching to CAD. Neither route silently falls back to the other.
Cancelling the waiter never releases the CAD lease before native work/cleanup.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Callable

from .storage import CadLease


class RenderValidationError(RuntimeError):
    def __init__(self, code: str, message: str, *, blocked: bool = False):
        self.code = code
        self.blocked = blocked
        super().__init__(message)


def _require_ok(value: dict, default_code: str) -> dict:
    if not isinstance(value, dict) or value.get("ok") is not True:
        value = value if isinstance(value, dict) else {}
        raise RenderValidationError(value.get("code", value.get("error_code", default_code)),
                                    str(value.get("error", value.get("message", default_code))))
    return value


def _same_document(left, right) -> bool:
    """Compare COM identities on the worker, never titles alone."""
    if left is None or right is None:
        return False
    from .native.session import same_object
    return same_object(left, right)


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class _ScratchFixture:
    """All document references and their dereferences stay on the native STA."""

    def __init__(self, backend, path: Path):
        self.backend, self.path = backend, path
        self.document = None
        self.creation_attempted = False

    async def prepare(self) -> dict:
        def create():
            session = self.backend.upstream._session
            # Direct inventory avoids run_guarded's UI/history handling on a
            # pre-existing target. Do not activate, rebuild or close user files.
            inventory = _require_ok(session.list_documents(), "DOCUMENT_INVENTORY_FAILED")
            documents = inventory.get("documents")
            if not isinstance(documents, list) or inventory.get("count") != len(documents):
                raise RenderValidationError("DOCUMENT_INVENTORY_UNKNOWN", "Cannot establish an empty session.",
                                            blocked=True)
            if documents:
                raise RenderValidationError("OPEN_DOCUMENTS_PRESENT",
                    "Add-in acceptance requires an empty session. No existing document was changed or closed.",
                    blocked=True)
            # A previous external close can leave an obsolete adapter target.
            # There is no open document here, so retaining that pointer cannot
            # preserve user state and would make the history guard dereference it.
            session._model = None
            self.creation_attempted = True
            created = _require_ok(session.run_guarded(session.new_part), "FIXTURE_CREATE_FAILED")
            self.document = session._model
            if self.document is None or self.document.GetTitle() != created.get("title"):
                raise RenderValidationError("FIXTURE_OWNERSHIP_UNKNOWN", "New part ownership could not be established.")
            _require_ok(session.run_guarded(session.add_box, width_mm=40, height_mm=25, depth_mm=8),
                        "FIXTURE_GEOMETRY_FAILED")
            _require_ok(session.run_guarded(session.save_part, path=str(self.path)), "FIXTURE_SAVE_FAILED")
            if (not self.path.is_file() or self.path.is_symlink()
                    or Path(self.document.GetPathName()).resolve() != self.path):
                raise RenderValidationError("FIXTURE_PATH_MISMATCH", "Saved fixture did not match its owned path.")
            measured = _require_ok(session.get_mass_properties(), "FIXTURE_MEASUREMENT_FAILED")
            volume = measured.get("mass_properties", {}).get("volume_mm3")
            if (type(volume) not in (int, float) or not math.isfinite(volume)
                    or not math.isclose(volume, 8000, rel_tol=1e-6, abs_tol=1e-6)):
                raise RenderValidationError("FIXTURE_VOLUME_MISMATCH", "Expected a real 8000 mm3 scratch solid.")
            return {"ok": True, "path": str(self.path), "title": self.document.GetTitle(),
                    "volume_mm3": volume, "expected_volume_mm3": 8000,
                    "initial_document_count": 0, "saved": True}
        return await self.backend._workflow_native(create)

    async def cleanup(self) -> dict:
        if not self.creation_attempted:
            return {"ok": True, "verified": True, "attempted": False, "closed": [],
                    "note": "No fixture creation was attempted."}

        def close_owned():
            session = self.backend.upstream._session
            documents = session._open_documents()
            if self.document is None:
                return {"ok": not documents, "verified": not documents, "attempted": False, "closed": [],
                        "code": None if not documents else "FIXTURE_OWNERSHIP_UNKNOWN",
                        "note": "Unknown documents are never closed after a failed creation."}
            owned = []
            for document in documents:
                path = document.GetPathName()
                if _same_document(document, self.document) or (path and Path(path).resolve() == self.path):
                    if path and Path(path).resolve() != self.path:
                        raise RenderValidationError("FIXTURE_MOVED", "Fixture path changed; it was left open.")
                    owned.append(document)
            if len(owned) > 1:
                raise RenderValidationError("FIXTURE_OWNERSHIP_UNKNOWN", "Ambiguous owned document; nothing closed.")
            closed = []
            if owned:
                title = owned[0].GetTitle()
                if sum(d.GetTitle() == title for d in documents) != 1:
                    raise RenderValidationError("FIXTURE_OWNERSHIP_UNKNOWN", "Document title is ambiguous; nothing closed.")
                session.close_document(owned[0])
                closed.append(title)
            remaining = session._open_documents()
            still_open = any(_same_document(d, self.document)
                             or (d.GetPathName() and Path(d.GetPathName()).resolve() == self.path)
                             for d in remaining)
            if not still_open and _same_document(session._model, self.document):
                session._model = None
            return {"ok": not still_open, "verified": not still_open, "attempted": bool(owned),
                    "closed": closed, "remaining_document_count": len(remaining),
                    "unowned_documents_closed": False, "saved_on_close": False}
        return await self.backend._workflow_native(close_owned)


def verify_render_image(response: dict, output_directory: Path, width: int, height: int) -> dict:
    """Independently decode one new PNG; dispatch success alone is insufficient."""
    from PIL import Image

    _require_ok(response, "RENDER_FAILED")
    images = response.get("images")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise RenderValidationError("RENDER_OUTPUT_MISSING", "Expected exactly one reported native PNG.")
    reported = images[0]
    path = Path(reported.get("path", ""))
    output = output_directory.resolve(strict=True)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to(output)):
        raise RenderValidationError("RENDER_OUTPUT_OUTSIDE_JOB", "PNG must be a regular file inside this probe's output.")
    path = path.resolve(strict=True)
    size = path.stat().st_size
    if not 0 < size <= 128 * 1024 * 1024:
        raise RenderValidationError("RENDER_OUTPUT_INVALID", "PNG is empty or exceeds the verification byte limit.")
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise RenderValidationError("RENDER_OUTPUT_INVALID", "Decoded PNG dimensions/format do not match.")
            image.verify()
        with Image.open(path) as image:
            image.load()
            mode = image.mode
    except (OSError, ValueError) as exc:
        raise RenderValidationError("RENDER_OUTPUT_INVALID", "PNG cannot be fully decoded.") from exc
    digest = _sha(path)
    if reported.get("sha256") not in (None, digest):
        raise RenderValidationError("RENDER_OUTPUT_CHANGED", "PNG hash changed after the renderer reported it.")
    return {"path": str(path), "format": "PNG", "width": width, "height": height,
            "mode": mode, "bytes": size, "sha256": digest, "alpha_tested": False}


async def validate_render_live(
    output: Path, *, route: str = "addin", project_path: Path | None = None,
    width: int = 640, height: int = 360, passes: int = 16, denoiser: bool = True,
    load_addin: bool = False, backend=None, project_renderer: Callable | None = None,
    lease_factory: Callable | None = None,
) -> dict:
    """Return and save terminal acceptance evidence; failed probes never pass.

    ``route='project'`` requires an existing SVPJ. It verifies that source's hash
    before/after rendering a copy, and does not establish that CAD export or the
    add-in macro works. ``backend`` is optional/injected and remains caller-owned.
    The lease factory is injectable for non-native tests; production uses CadLease.
    """
    if route not in {"addin", "project"}:
        raise ValueError("route must be addin or project; automatic fallback is not supported")
    for name, value, lower, upper in (("width", width, 128, 4096), ("height", height, 128, 4096),
                                      ("passes", passes, 1, 5000)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in [{lower},{upper}]")
    if type(denoiser) is not bool or type(load_addin) is not bool:
        raise ValueError("denoiser and load_addin must be booleans")
    if (route == "project") != (project_path is not None):
        raise ValueError("project_path is required only for the project route")
    if route == "project" and (load_addin or backend is not None):
        raise ValueError("Project validation does not attach to CAD or load its add-in")
    source = None
    if project_path is not None:
        source = Path(project_path)
        if (not source.is_absolute() or source.suffix.lower() != ".svpj" or source.is_symlink()
                or not source.is_file() or source.stat().st_size > 512 * 1024 * 1024):
            raise ValueError("project_path must be an existing regular absolute SVPJ of at most 512 MiB")
        source = source.resolve(strict=True)
    output = Path(output)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError("Choose a new absolute output directory")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence = {"kind": "native_render_acceptance", "route": route, "ok": False, "status": "failed",
                "render_verified": False, "addin_render_verified": False, "project_render_verified": False,
                "macro_render_verified": False, "alpha_tested": False, "visual_review_required": True,
                "settings": {"width": width, "height": height, "passes": passes, "denoiser": denoiser},
                "started_at": datetime.now(timezone.utc).isoformat(), "stages": [],
                "evidence_path": str(output / "evidence.json"),
                "limits": ["PNG integrity, dimensions and hash are checked; visual fidelity is not approved.",
                           "Project rendering does not establish add-in rendering or new CAD-to-SVPJ export.",
                           "A single scratch solid is not a general model or GPU performance benchmark."]}

    async def run():
        nonlocal backend
        owned_backend = False
        fixture = None
        source_hash = None
        started = time.perf_counter()

        async def stage(name, awaitable):
            before = time.perf_counter()
            record = {"stage": name, "status": "running"}
            evidence["stages"].append(record)
            try:
                value = await awaitable
                record.update(status="completed", result=value)
                return value
            except Exception as exc:
                record.update(status="failed", code=getattr(exc, "code", "RENDER_VALIDATION_FAILED"), error=str(exc))
                raise
            finally:
                record["duration_ms"] = round((time.perf_counter() - before) * 1000, 3)

        async def perform():
            nonlocal fixture, owned_backend, backend, source_hash
            if route == "addin":
                if backend is None:
                    from .backend import NativeBackend
                    backend = NativeBackend()
                    owned_backend = True
                fixture = _ScratchFixture(backend, output / "RenderProbe.sldprt")
                evidence["fixture"] = await stage("fixture", fixture.prepare())
                availability = await stage("visualize_status", backend.visualize("enable" if load_addin else "status"))
                _require_ok(availability, "VISUALIZE_UNAVAILABLE")
                _require_ok(await stage("frame", backend.visualize("configure_camera",
                    document_path=str(fixture.path), action="apply", view="isometric", fit=True)), "CAMERA_FAILED")
                result = await stage("render", backend.visualize("render", document_path=str(fixture.path),
                    output_dir=output / "render", width=width, height=height, passes=passes,
                    denoiser=denoiser, image_format="png", job_name="probe"))
                _require_ok(result, "RENDER_FAILED")
                execution = result.get("execution")
                if execution not in {"solidworks_in_process_macro", "direct_api"}:
                    raise RenderValidationError("RENDER_ROUTE_UNVERIFIED", "Add-in execution route is not reported.")
                evidence["execution"] = execution
            else:
                source_hash = _sha(source)
                evidence["source_project"] = {"path": str(source), "sha256_before": source_hash}
                renderer = project_renderer
                if renderer is None:
                    from .project_render_bridge import render
                    renderer = render
                result = await stage("render", asyncio.to_thread(renderer, project_path=source,
                    output_directory=output / "render", width=width, height=height,
                    passes=passes, denoiser=denoiser))
                _require_ok(result, "RENDER_FAILED")
                if not isinstance(result.get("renderer"), dict) or result["renderer"].get("native_execution") is not True:
                    raise RenderValidationError("RENDER_ROUTE_UNVERIFIED", "Native project execution is not reported.")
                evidence["execution"] = "native_offline_renderer_project_copy"
            image = verify_render_image(result, output / "render", width, height)
            evidence.update(render_verified=True, images=[image], status="passed", ok=True)
            evidence[f"{route}_render_verified"] = True
            evidence["macro_render_verified"] = evidence["execution"] == "solidworks_in_process_macro"

        # Protect the whole job, including cleanup, rather than a single dispatch.
        # A cancelled caller awaits this task through the barrier below.
        try:
            with (lease_factory or CadLease)():
                try:
                    await perform()
                finally:
                    if fixture is not None:
                        try:
                            cleanup = await stage("cleanup", fixture.cleanup())
                            evidence["cleanup"] = cleanup
                            if cleanup.get("verified") is not True or cleanup.get("ok") is not True:
                                evidence.update(ok=False, status="indeterminate", code="FIXTURE_CLEANUP_UNVERIFIED")
                        except Exception as exc:
                            evidence["cleanup"] = {"ok": False, "verified": False, "error": str(exc),
                                                   "code": getattr(exc, "code", "FIXTURE_CLEANUP_FAILED")}
                            evidence.update(ok=False, status="indeterminate", code="FIXTURE_CLEANUP_UNVERIFIED")
                    else:
                        evidence["cleanup"] = {"ok": True, "verified": True, "attempted": False,
                                               "note": "No CAD fixture was created."}
                    if source_hash is not None:
                        try:
                            after = _sha(source)
                            unchanged = after == source_hash
                            evidence["source_project"].update(sha256_after=after, unchanged=unchanged)
                        except Exception as exc:
                            unchanged = False
                            evidence["source_project"].update(unchanged=False, error=str(exc))
                        if not unchanged:
                            evidence.update(ok=False, status="indeterminate", code="SOURCE_PROJECT_CHANGED")
        except Exception as exc:
            failure = {"code": getattr(exc, "code", "RENDER_VALIDATION_FAILED"), "error": str(exc)}
            if evidence["status"] == "indeterminate":
                evidence["primary_failure"] = failure
            else:
                evidence.update(ok=False, status="blocked" if getattr(exc, "blocked", False) else "failed", **failure)
        finally:
            if owned_backend:
                try:
                    backend.close()
                except Exception as exc:
                    evidence.update(ok=False, status="indeterminate", code="BACKEND_CLOSE_FAILED",
                                    backend_close_error=str(exc))
            evidence["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
            (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=True, indent=2), encoding="utf-8")
        return evidence

    task = asyncio.create_task(run())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()
    return result


def validation_exit_code(evidence: dict) -> int:
    """Unavailable or unverified rendering can never be a successful CLI exit."""
    if evidence.get("ok") is True and evidence.get("render_verified") is True and evidence.get("status") == "passed":
        return 0
    return 2 if evidence.get("status") == "blocked" else 1
