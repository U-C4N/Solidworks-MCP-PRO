"""Legacy entry point delegates to the independent native transaction owner."""
from types import SimpleNamespace

import pytest

from solidworks_mcp_pro.native_guard import run_guarded


def test_guard_delegates_arguments_and_preserves_receipt():
    calls = []
    result = {"ok": True, "native_receipt": {"undo_recorded": True}}
    def transaction(function, *args, **kwargs):
        calls.append((function, args, kwargs))
        return result
    def function():
        return None
    assert run_guarded(SimpleNamespace(run_guarded=transaction), function, 7, value=3) is result
    assert calls == [(function, (7,), {"value": 3})]


def test_guard_does_not_reinterpret_native_failures():
    error = RuntimeError("native restoration failed")
    def transaction(*args, **kwargs):
        raise error
    with pytest.raises(RuntimeError) as caught:
        run_guarded(SimpleNamespace(run_guarded=transaction), lambda: None)
    assert caught.value is error
