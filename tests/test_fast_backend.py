"""Native workflow boundaries never retarget a different active document."""
import asyncio
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro import camera_bridge, pose_bridge, render_state_bridge, visualize_bridge
from solidworks_mcp_pro import diagnostics


class Worker:
    def submit(self, fn):
        future = Future()
        try:
            future.set_result(fn())
        except BaseException as exc:
            future.set_exception(exc)
        return future


def backend_with_session():
    calls = []
    def refuse():
        raise AssertionError("Exact-path workflows must not call _require_model")
    session = SimpleNamespace(_ensure=lambda: "active-app", _require_model=refuse,
                              _command_in_progress=lambda value: calls.append(value) or False,
                              _model=None)
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_session=session, _worker=Worker())
    return backend, calls


@pytest.mark.parametrize("operation,values", [
    ("render", {"passes": 50}),
    ("export_project", {"path": "owned.svpj", "grouping": "part"}),
])
async def test_exact_render_verifies_active_document_and_consumes_path(monkeypatch, operation, values):
    backend, _ = backend_with_session()
    seen = []
    monkeypatch.setattr(render_state_bridge, "_model", lambda app, path: seen.append((app, path)))
    monkeypatch.setattr(visualize_bridge, operation, lambda app, **kwargs: {"ok": True, "kwargs": kwargs})
    result = await backend.visualize(operation, document_path="A.sldasm", **values)
    assert seen == [("active-app", "A.sldasm")]
    assert result["kwargs"] == values


@pytest.mark.parametrize("operation", ["render", "export_project"])
async def test_mismatched_document_never_renders(monkeypatch, operation):
    backend, _ = backend_with_session()
    def refuse(*args):
        raise RuntimeError("DOCUMENT_MISMATCH")
    monkeypatch.setattr(render_state_bridge, "_model", refuse)
    monkeypatch.setattr(visualize_bridge, operation, lambda *args, **kwargs: pytest.fail("must not render/export"))
    with pytest.raises(RuntimeError, match="DOCUMENT_MISMATCH"):
        await backend.visualize(operation, document_path="A.sldasm")


async def test_exact_camera_and_pose_skip_target_activation(monkeypatch):
    backend, flags = backend_with_session()
    monkeypatch.setattr(camera_bridge, "configure_camera", lambda app, **kwargs: {"ok": True})
    monkeypatch.setattr(pose_bridge, "pose", lambda app, **kwargs: {"ok": True})
    assert (await backend.visualize("configure_camera", document_path="A.sldasm", _exact_active=True))["ok"]
    assert (await backend.animation("pose", document_path="A.sldasm", transforms=[], _exact_active=True))["ok"]
    assert flags == [True, False]


async def test_repeated_native_task_cancellation_drains_before_return():
    pending = Future()
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=SimpleNamespace(submit=lambda fn: pending))
    task = asyncio.create_task(backend._workflow_native(lambda: {"ok": True}))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not pending.cancelled()
    pending.set_result({"ok": True})
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_health_batches_reads_in_command_mode_and_restores_it(monkeypatch):
    backend, flags = backend_with_session()
    monkeypatch.setattr(diagnostics, "diagnose", lambda session, **kwargs: {"ok": True, "healthy": True})
    result = await backend.health("diagnose", document_path="model.sldprt")
    assert result["healthy"] and flags == [True, False]


async def test_diagnostic_exception_still_restores_command_mode(monkeypatch):
    backend, flags = backend_with_session()
    def fail(*args, **kwargs):
        raise RuntimeError("diagnostic failed")
    monkeypatch.setattr(diagnostics, "diagnose", fail)
    with pytest.raises(RuntimeError, match="diagnostic failed"):
        await backend.health("diagnose", document_path="model.sldprt")
    assert flags == [True, False]
