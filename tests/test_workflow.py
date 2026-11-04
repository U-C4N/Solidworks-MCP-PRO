import asyncio
from contextlib import contextmanager
import json

import pytest

from solidworks_mcp_pro.workflow import WorkflowEngine


SCHEMAS = {
    "add_box": {
        "type": "object", "properties": {"width": {"type": "number", "exclusiveMinimum": 0}},
        "required": ["width"], "additionalProperties": False,
    },
    "get_status": {"type": "object", "additionalProperties": False},
    "pro_plan": {"type": "object"},
    "run_vba": {"type": "object"},
}


def step(width=10):
    return {"tool": "add_box", "arguments": {"width": width}}


def run(coro):
    return asyncio.run(coro)


def recorder(response=None):
    calls = []

    async def invoke(name, arguments):
        calls.append((name, arguments))
        return {"ok": True} if response is None else response

    return calls, invoke


def test_dry_run_validates_without_executing_or_reserving_id():
    calls, invoke = recorder()
    engine = WorkflowEngine(SCHEMAS, invoke)
    result = run(engine.execute([step()], request_id="same"))
    assert result["ok"] and result["status"] == "validated"
    assert not calls
    assert run(engine.execute([step(20)], request_id="same", dry_run=False))["ok"]
    assert calls == [("add_box", {"width": 20})]


@pytest.mark.parametrize("steps", [
    [step(), step(-1)],
    [{"tool": "missing", "arguments": {}}],
    [{"tool": "pro_plan", "arguments": {}}],
    [{"tool": "run_vba", "arguments": {}}],
    [{"tool": "get_status", "arguments": [],}],
    [{"tool": "get_status", "arguments": {}, "extra": 1}],
    [step(float("nan"))], [step(float("inf"))],
    [step((1, 2))], [step({1: 2})], [], [step()] * 33,
])
def test_invalid_entire_plan_prevents_every_call(steps):
    calls, invoke = recorder()
    engine = WorkflowEngine(SCHEMAS, invoke)
    result = run(engine.execute(steps, request_id="bad", dry_run=False))
    assert result["status"] == "rejected"
    assert not calls


def test_limits_and_request_id_validation():
    calls, invoke = recorder()
    engine = WorkflowEngine({"text": {"type": "object"}}, invoke)
    big = [{"tool": "text", "arguments": {"data": "x" * 262144}}]
    assert "256 KiB" in run(engine.execute(big, request_id="big"))["error"]
    for identifier in ("", "a" * 129, "bad\n", "../file", None):
        assert not run(engine.execute([step()], request_id=identifier))["ok"]
    assert not run(engine.execute([step()], request_id="ok", dry_run=1))["ok"]
    assert not calls


def test_idempotency_replays_immutable_result_and_rejects_different_plan():
    calls, invoke = recorder({"ok": True, "values": [1]})
    engine = WorkflowEngine(SCHEMAS, invoke)
    first = run(engine.execute([step()], request_id="once", dry_run=False))
    first["steps"][0]["result"]["values"].append(2)
    replay = run(engine.execute([step()], request_id="once", dry_run=False))
    assert replay["replayed"]
    assert replay["steps"][0]["result"]["values"] == [1]
    mismatch = run(engine.execute([step(2)], request_id="once", dry_run=False))
    assert mismatch["status"] == "rejected"
    assert len(calls) == 1


def test_failure_stops_after_first_failed_step_and_does_not_rollback():
    calls = []

    async def invoke(name, arguments):
        calls.append(arguments["width"])
        return {"ok": len(calls) != 2}

    engine = WorkflowEngine(SCHEMAS, invoke)
    result = run(engine.execute([step(1), step(2), step(3)], request_id="fail", dry_run=False))
    assert calls == [1, 2]
    assert result["status"] == "failed" and result["completed_steps"] == 1
    assert result["failed_step"] == 2 and result["rollback"] == "not_attempted"
    assert run(engine.execute([step(1), step(2), step(3)], request_id="fail", dry_run=False))["replayed"]
    assert calls == [1, 2]


def test_mcp_is_error_stops_plan():
    calls, invoke = recorder({"isError": True})
    result = run(WorkflowEngine(SCHEMAS, invoke).execute([step(), step()], request_id="mcp", dry_run=False))
    assert result["status"] == "failed" and len(calls) == 1


def test_exception_is_recorded_without_retry():
    async def invoke(name, arguments):
        raise RuntimeError("COM disconnected")

    engine = WorkflowEngine(SCHEMAS, invoke)
    result = run(engine.execute([step()], request_id="exception", dry_run=False))
    assert result["status"] == "failed"
    assert result["steps"][0]["result"]["outcome_may_be_indeterminate"]


def test_concurrent_plans_are_serial_and_same_id_deduplicates():
    async def scenario():
        calls = []

        async def invoke(name, arguments):
            calls.append(arguments["width"])
            await asyncio.sleep(0)
            return {"ok": True}

        engine = WorkflowEngine(SCHEMAS, invoke)
        results = await asyncio.gather(
            engine.execute([step(1), step(2)], request_id="a", dry_run=False),
            engine.execute([step(3), step(4)], request_id="b", dry_run=False),
            engine.execute([step(1), step(2)], request_id="a", dry_run=False),
        )
        assert calls == [1, 2, 3, 4]
        assert results[2]["replayed"]

    run(scenario())


def test_cancelled_call_is_indeterminate_and_never_reexecuted():
    async def scenario():
        started = asyncio.Event()
        calls = []

        async def invoke(name, arguments):
            calls.append(arguments)
            started.set()
            await asyncio.Event().wait()

        engine = WorkflowEngine(SCHEMAS, invoke)
        task = asyncio.create_task(engine.execute([step(), step()], request_id="cancelled", dry_run=False))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        replay = await engine.execute([step(), step()], request_id="cancelled", dry_run=False)
        assert replay["status"] == "indeterminate" and replay["replayed"]
        assert len(calls) == 1 and replay["completed_steps"] == 0

    run(scenario())


def test_audit_jsonl_has_outcomes_without_arguments(tmp_path):
    path = tmp_path / "nested" / "audit.jsonl"
    calls, invoke = recorder()
    engine = WorkflowEngine(SCHEMAS, invoke, path)
    assert run(engine.execute([step(123.456)], request_id="audit", dry_run=False))["ok"]
    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [item["event"] for item in records] == ["plan_started", "step_started", "step_finished", "plan_finished"]
    assert records[2]["duration_ms"] >= 0
    assert "123.456" not in path.read_text(encoding="utf-8")
    assert engine.recent(2) == records[-2:]
    recent = engine.recent()
    recent[0]["event"] = "changed"
    assert engine.recent()[0]["event"] == "plan_started"
    for limit in (0, 257, True):
        with pytest.raises(ValueError):
            engine.recent(limit)


def test_audit_failure_is_explicit_but_does_not_lose_cad_success(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("exists", encoding="utf-8")
    calls, invoke = recorder()
    engine = WorkflowEngine(SCHEMAS, invoke, blocker / "audit.jsonl")
    result = run(engine.execute([step()], request_id="audit-fail", dry_run=False))
    assert result["ok"] and result["audit_warnings"] and len(calls) == 1
    assert len(engine.recent()) == 4


def test_cache_and_audit_are_bounded_without_eviction_of_request_ids(monkeypatch):
    monkeypatch.setattr("solidworks_mcp_pro.workflow.MAX_REQUESTS", 2)
    calls, invoke = recorder()
    engine = WorkflowEngine(SCHEMAS, invoke)
    for identifier in ("a", "b"):
        assert run(engine.execute([step()], request_id=identifier, dry_run=False))["ok"]
    assert run(engine.execute([step()], request_id="c", dry_run=False))["status"] == "rejected"
    assert run(engine.execute([step()], request_id="a", dry_run=False))["replayed"]
    assert len(calls) == 2
    for index in range(300):
        run(engine.execute([step()], request_id=f"dry-{index}"))
    assert len(engine.recent(256)) == 256


def test_descriptor_input_schema_supported():
    calls, invoke = recorder()
    engine = WorkflowEngine({"add_box": {"inputSchema": SCHEMAS["add_box"]}}, invoke)
    assert run(engine.execute([step()], request_id="descriptor", dry_run=False))["ok"]


@pytest.mark.parametrize("name", ["run_code", "powershell.run", "run-command", "shell", "execute_python"])
def test_code_execution_tools_cannot_enter_plans(name):
    calls, invoke = recorder()
    engine = WorkflowEngine({name: {"type": "object"}}, invoke)
    result = run(engine.execute([{"tool": name, "arguments": {}}], request_id="code", dry_run=False))
    assert result["status"] == "rejected" and not calls


def test_execution_scope_is_acquired_after_serial_lock_and_released_on_failure():
    events = []
    calls, invoke = recorder({"ok": False})

    @contextmanager
    def scope():
        assert engine.execution_lock.locked()
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    engine = WorkflowEngine(SCHEMAS, invoke, execution_scope=scope)
    assert run(engine.execute([step()], request_id="dry"))["ok"]
    assert not events
    result = run(engine.execute([step()], request_id="scope", dry_run=False))
    assert result["status"] == "failed"
    assert events == ["enter", "exit"]
    assert not engine.execution_lock.locked()
