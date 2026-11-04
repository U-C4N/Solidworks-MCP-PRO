"""Native routing contracts using a fake worker; no SOLIDWORKS connection."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import camera_bridge, scene_bridge, visualize_bridge
from solidworks_mcp_pro.backend import NativeBackend


class Worker:
    def __init__(self):
        self.inside = False
        self.submitted = 0

    def submit(self, fn):
        self.submitted += 1
        future = Future()
        self.inside = True
        try:
            future.set_result(fn())
        except Exception as exc:
            future.set_exception(exc)
        finally:
            self.inside = False
        return future


@pytest.mark.parametrize("operation, module, method, arguments, require_model", [
    ("configure_camera", camera_bridge, "configure_camera", {"document_path": "model.sldasm", "action": "inspect"}, True),
    ("apply_scene", scene_bridge, "apply", {"document_path": "model.sldasm", "dry_run": True}, True),
    ("open_project", visualize_bridge, "open_project", {"path": "model.svpj"}, False),
    ("render", visualize_bridge, "render", {"output_dir": "render"}, True),
    ("export_project", visualize_bridge, "export_project", {"path": "model.svpj"}, True),
])
async def test_visualize_routes_on_worker_without_action_keyword_collision(monkeypatch, operation, module, method, arguments, require_model):
    worker = Worker()
    app = object()
    calls = []
    session = SimpleNamespace(_ensure=lambda: calls.append("ensure") or app,
                              _require_model=lambda: calls.append("model"))

    def target(actual_app, **kwargs):
        assert worker.inside and actual_app is app
        calls.append((method, kwargs))
        return {"ok": True, "method": method}

    monkeypatch.setattr(module, method, target)
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker, _session=session)
    result = await backend.visualize(operation, **arguments)
    assert result["ok"] and worker.submitted == 1
    assert calls == ["ensure", *(["model"] if require_model else []), (method, arguments)]


@pytest.mark.parametrize("operation", ["configure_camera", "apply_scene"])
async def test_missing_session_model_prevents_scene_or_camera_bridge(monkeypatch, operation):
    worker = Worker()

    def missing():
        raise RuntimeError("No current model")

    monkeypatch.setattr(camera_bridge, "configure_camera", lambda *args, **kwargs: pytest.fail("must not reach bridge"))
    monkeypatch.setattr(scene_bridge, "apply", lambda *args, **kwargs: pytest.fail("must not reach bridge"))
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker,
        _session=SimpleNamespace(_ensure=lambda: object(), _require_model=missing))
    with pytest.raises(RuntimeError, match="No current model"):
        await backend.visualize(operation, document_path="model.sldasm")
