"""Read-only optimization keeps the STA and assembly mutation guard."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.backend import NativeBackend


class Worker:
    def __init__(self):
        self.submitted = 0

    def submit(self, fn):
        self.submitted += 1
        future = Future()
        future.set_result(fn())
        return future


@pytest.mark.parametrize("kind, guarded", [(1, False), (2, True)])
async def test_mass_read_retains_assembly_guard(kind, guarded):
    calls = []
    def mass():
        calls.append("mass")
        return {"ok": True, "mass_properties": {"volume_mm3": 1000}}
    def guard(fn, **kwargs):
        calls.append("guard")
        return fn(**kwargs)
    worker = Worker()
    session = SimpleNamespace(_reattach_if_gone=lambda: calls.append("reattach"),
        _require_model=lambda: SimpleNamespace(GetType=lambda: kind), get_mass_properties=mass,
        run_guarded=guard)
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=worker, _session=session,
        _failure=lambda exc: {"ok": False, "error": str(exc)})
    result = await backend.call("get_mass_properties", {})
    assert result["mass_properties"]["volume_mm3"] == 1000
    assert ("guard" in calls) == guarded
    assert calls[0] == "reattach"
    assert worker.submitted == 1


async def test_fast_read_failure_is_not_success():
    def fail():
        raise RuntimeError("disconnected")
    backend = NativeBackend.__new__(NativeBackend)
    backend.upstream = SimpleNamespace(_worker=Worker(), _session=SimpleNamespace(_reattach_if_gone=fail),
        _failure=lambda exc: {"ok": False, "error": str(exc)})
    result = await backend.call("get_status", {})
    assert result == {"ok": False, "error": "disconnected"}
