"""Pinned upstream adapter. All CAD work stays on upstream's COM STA worker."""
from __future__ import annotations

import json
import asyncio
from types import MethodType
from typing import Any

from mcp import types

from .validation import guard_native_write, normalize_args, validate_result


class NativeBackend:
    def __init__(self) -> None:
        # Import lazily: offline previews and --doctor do not need a COM session.
        import solidworks_mcp.server as upstream
        self.upstream = upstream
        from .diagnostics import RepairPlans
        self.repair_plans = RepairPlans()
        if getattr(upstream, "_worker", None) is not None:
            from .native_guard import run_guarded
            # The engine is pinned to 0.19.0. Adapt only this session instance;
            # upstream source and independently launched rivals stay unchanged.
            upstream._session.run_guarded = MethodType(run_guarded, upstream._session)

    @property
    def instructions(self) -> str:
        return self.upstream.INSTRUCTIONS

    async def list_tools(self) -> list[types.Tool]:
        return await self.upstream.mcp.list_tools()

    async def call(self, name: str, arguments: dict[str, Any]) -> dict:
        arguments = guard_native_write(name, normalize_args(name, arguments))
        worker = getattr(self.upstream, "_worker", None)
        if worker is not None and name in {"get_status", "get_mass_properties"}:
            def read():
                session = self.upstream._session
                try:
                    session._reattach_if_gone()
                    method = getattr(session, name)
                    # Assembly mass includes an EditRebuild during bounding-box
                    # evaluation. Keep the original guard for that path.
                    if name == "get_mass_properties" and int(session._require_model().GetType()) != 1:
                        return session.run_guarded(method, **arguments)
                    return method(**arguments)
                except Exception as exc:
                    return self.upstream._failure(exc)
            return validate_result(await asyncio.shield(asyncio.wrap_future(worker.submit(read))))
        try:
            result = await self.upstream.mcp.call_tool(name, arguments)
        finally:
            # Upstream's 120s await timeout cannot stop COM. Drain the queue before
            # releasing the outer lease, even if upstream reported a timeout.
            worker = getattr(self.upstream, "_worker", None)
            if worker is not None:
                await asyncio.shield(asyncio.wrap_future(worker.submit(lambda: None)))
        if isinstance(result, dict):
            return validate_result(result)
        if isinstance(result, tuple) and len(result) == 2:
            if isinstance(result[1], dict):
                return validate_result(result[1])
            result = result[0]
        for block in result:
            if isinstance(block, types.TextContent):
                try:
                    value = json.loads(block.text)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    return validate_result(value)
        raise RuntimeError("Unexpected upstream response; refusing to report success")

    async def visualize(self, operation: str, **kwargs) -> dict:
        from . import visualize_bridge
        worker = getattr(self.upstream, "_worker", None)
        if worker is None:
            return {"ok": False, "error": "Visualize requires Windows and SOLIDWORKS 2025 or newer"}

        def run():
            session = self.upstream._session
            app = session._ensure()
            values = dict(kwargs)
            exact_active = values.pop("_exact_active", False)
            exact_bridge = exact_active and operation in {"apply_appearances", "apply_scene", "configure_camera", "named_camera"}
            exact_document = operation in {"render", "export_project"} and "document_path" in values
            if exact_document:
                from .render_state_bridge import _model
                _model(app, values.pop("document_path"))
            if operation not in ("status", "enable", "open_project", "render_project") and not exact_bridge and not exact_document:
                session._require_model()
            if operation == "render_project":
                from . import project_render_bridge
                return project_render_bridge.render(**values)
            if operation == "apply_appearances":
                from . import appearance_bridge
                return appearance_bridge.apply(app, **values)
            if operation == "apply_scene":
                from . import scene_bridge
                return scene_bridge.apply(app, **values)
            if operation == "configure_camera":
                from . import camera_bridge
                return camera_bridge.configure_camera(app, **values)
            if operation == "named_camera":
                from . import named_camera_bridge
                return named_camera_bridge.configure(app, **values)
            return getattr(visualize_bridge, operation)(app, **values)

        return await asyncio.shield(asyncio.wrap_future(worker.submit(run)))

    def guide(self) -> str:
        return self.upstream.guide()

    async def _workflow_native(self, function) -> dict:
        """Submit once and retain native ownership through cancellation.

        The enclosing operation owns the CAD lease. A cancelled await must not
        allow its cleanup to race a still-running STA mutation.
        """
        worker = getattr(self.upstream, "_worker", None)
        if worker is None:
            return {"ok": False, "code": "NATIVE_UNAVAILABLE", "error": "Windows and SOLIDWORKS required"}
        future = asyncio.wrap_future(worker.submit(function))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                if future.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def health(self, operation: str, **kwargs) -> dict:
        def run():
            from .diagnostics import diagnose
            session = self.upstream._session
            previous = None
            try:
                # The application flag batches COM reads without touching the
                # model, activating documents or creating an undo transaction.
                try:
                    previous = session._command_in_progress(True)
                except Exception:
                    if operation == "apply":
                        raise
                if operation == "diagnose":
                    return diagnose(session, **kwargs)
                if operation == "plan":
                    return self.repair_plans.plan(session, **kwargs)
                if operation == "apply":
                    return self.repair_plans.apply(session, **kwargs)
                raise ValueError("Unsupported health operation")
            finally:
                if previous is not None:
                    session._command_in_progress(previous)
        return await self._workflow_native(run)

    async def colorize(self, **kwargs) -> dict:
        def run():
            from .color_bridge import colorize
            session = self.upstream._session
            app = session._ensure()
            previous = session._command_in_progress(True)
            try:
                return colorize(app, **kwargs)
            finally:
                session._command_in_progress(previous)
        return await self._workflow_native(run)

    async def design_validate(self, **kwargs) -> dict:
        """Read exact active geometry without activating, rebuilding or saving."""
        def run():
            from .design_validation import validate_design
            return validate_design(self.upstream._session._sw, **kwargs)
        return await self._workflow_native(run)

    async def variant_study(self, **kwargs) -> dict:
        """Run isolated part copies on the single native STA worker."""
        def run():
            from .variant_study import run_variant_study
            return run_variant_study(self.upstream._session._sw, **kwargs)
        return await self._workflow_native(run)

    async def camera_state(self, action: str, **kwargs) -> dict:
        def run():
            from .render_state_bridge import camera_state
            return camera_state(self.upstream._session._ensure(), action, **kwargs)
        return await self._workflow_native(run)

    async def pose_state(self, action: str, **kwargs) -> dict:
        if action != "restore":
            raise ValueError("Unsupported pose state operation")
        def run():
            from .render_state_bridge import restore_poses
            session = self.upstream._session
            previous = session._command_in_progress(True)
            try:
                return restore_poses(session._ensure(), **kwargs)
            finally:
                session._command_in_progress(previous)
        return await self._workflow_native(run)

    async def animation(self, operation: str, **kwargs) -> dict:
        worker = getattr(self.upstream, "_worker", None)
        if worker is None:
            return {"ok": False, "error": "Assembly animation requires Windows and SOLIDWORKS"}

        def run():
            from . import animation_bridge
            session = self.upstream._session
            app = session._ensure()
            values = dict(kwargs)
            exact_active = values.pop("_exact_active", False)
            if not exact_active or operation not in {"pose", "visibility", "bend"}:
                session._require_model()
            if operation in {"pose", "visibility", "bend"}:
                if operation == "pose":
                    from .pose_bridge import pose as target
                elif operation == "visibility":
                    from .visibility_bridge import visibility as target
                else:
                    from .bending_bridge import apply as target
                previous = session._command_in_progress(True)
                try:
                    return target(app, **values)
                finally:
                    session._command_in_progress(previous)
            if operation not in {"inspect", "configure", "clone"}:
                raise ValueError("Unsupported assembly animation operation")
            return getattr(animation_bridge, operation)(app, **values)

        return await asyncio.shield(asyncio.wrap_future(worker.submit(run)))

    def close(self) -> None:
        worker = getattr(self.upstream, "_worker", None)
        if worker is not None:
            worker.shutdown()
