import asyncio

import pytest

from solidworks_mcp_pro.operations import OperationRegistry


def run(coro):
    return asyncio.run(coro)


async def good():
    return {"ok": True, "values": [1]}


def test_result_is_json_safe_detached_and_enriched():
    async def scenario():
        registry = OperationRegistry()
        result = await registry.run("model", good)
        assert result["ok"] and result["status"] == "completed"
        assert result["operation_status"] == "completed"
        operation_id = result["operation_id"]
        result["values"].append(2)
        assert (await registry.poll(operation_id))["values"] == [1]
        metadata = registry.list_recent()[0]
        assert metadata["finished_at"] and metadata["duration_ms"] >= 0
        assert "values" not in metadata

    run(scenario())


def test_timeout_keeps_work_running_and_poll_returns_result():
    async def scenario():
        gate = asyncio.Event()
        calls = []

        async def factory():
            calls.append("started")
            await gate.wait()
            calls.append("finished")
            return {"ok": True, "volume_mm3": 100}

        registry = OperationRegistry()
        result = await registry.run("cad", factory, wait_seconds=0)
        assert result["status"] == "running"
        assert (await registry.poll(result["operation_id"]))["status"] == "running"
        gate.set()
        finished = await registry.poll(result["operation_id"], wait_seconds=1)
        assert finished["volume_mm3"] == 100
        assert calls == ["started", "finished"]

    run(scenario())


def test_cancelled_mcp_waiter_does_not_cancel_factory():
    async def scenario():
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        async def factory():
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
            calls.append("finished")
            return {"ok": True}

        registry = OperationRegistry()
        waiter = asyncio.create_task(registry.run("cad", factory))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        metadata = registry.list_recent()[0]
        assert metadata["status"] == "running"
        finish.set()
        result = await registry.poll(metadata["operation_id"], wait_seconds=1)
        assert result["ok"] and calls == ["finished"]

    run(scenario())


def test_cancelled_poll_does_not_cancel_factory():
    async def scenario():
        gate = asyncio.Event()

        async def factory():
            await gate.wait()
            return {"ok": True}

        registry = OperationRegistry()
        started = await registry.run("cad", factory, wait_seconds=0)
        waiter = asyncio.create_task(registry.poll(started["operation_id"], wait_seconds=20))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        gate.set()
        assert (await registry.poll(started["operation_id"], wait_seconds=1))["ok"]

    run(scenario())


def test_same_key_concurrently_submits_once_and_name_conflict_rejects():
    async def scenario():
        calls = []

        async def factory():
            calls.append(1)
            await asyncio.sleep(0)
            return {"ok": True}

        registry = OperationRegistry()
        results = await asyncio.gather(*(
            registry.run("plan", factory, key="once") for _ in range(5)
        ))
        assert len(calls) == 1
        assert len({result["operation_id"] for result in results}) == 1
        assert sum(result.get("replayed", False) for result in results) == 4
        conflict = await registry.run("other", factory, key="once")
        assert conflict["error"]["code"] == "OPERATION_KEY_CONFLICT"
        assert len(calls) == 1

    run(scenario())


def test_factory_exception_is_failed_and_keyed_replay_does_not_retry():
    async def scenario():
        calls = []

        async def factory():
            calls.append(1)
            raise RuntimeError("native call failed")

        registry = OperationRegistry()
        result = await registry.run("cad", factory, key="failure")
        assert not result["ok"] and result["status"] == "failed"
        assert result["error"]["code"] == "OPERATION_FAILED"
        assert result["outcome_may_be_indeterminate"]
        assert (await registry.run("cad", factory, key="failure"))["replayed"]
        assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize("value", [None, [], {"n": float("nan")}, {"data": object()}])
def test_invalid_factory_result_becomes_structured_failure(value):
    async def scenario():
        async def factory():
            return value

        result = await OperationRegistry().run("bad", factory)
        assert not result["ok"] and result["status"] == "failed"
        assert result["error"]["code"] == "OPERATION_FAILED"

    run(scenario())


def test_native_failure_and_workflow_status_are_preserved():
    async def scenario():
        async def factory():
            return {"ok": False, "status": "rejected", "error": "Invalid plan"}

        result = await OperationRegistry().run("plan", factory)
        assert result["status"] == "rejected" and result["operation_status"] == "failed"
        assert result["error"] == "Invalid plan"

    run(scenario())


def test_capacity_never_evicts_running_tasks():
    async def scenario():
        gate = asyncio.Event()

        async def factory():
            await gate.wait()
            return {"ok": True}

        registry = OperationRegistry(1)
        first = await registry.run("running", factory, wait_seconds=0)
        refused = await registry.run("new", good)
        assert refused["error"]["code"] == "OPERATION_CAPACITY"
        assert registry.list_recent()[0]["operation_id"] == first["operation_id"]
        gate.set()
        await registry.poll(first["operation_id"], wait_seconds=1)

    run(scenario())


def test_capacity_evicts_oldest_completed_unkeyed_only():
    async def scenario():
        registry = OperationRegistry(2)
        first = await registry.run("first", good)
        keyed = await registry.run("keyed", good, key="keep")
        third = await registry.run("third", good)
        assert third["ok"]
        assert (await registry.poll(first["operation_id"]))["error"]["code"] == "OPERATION_NOT_FOUND"
        assert (await registry.poll(keyed["operation_id"]))["ok"]
        assert len(registry.list_recent()) == 2
        assert registry.list_recent()[0]["name"] == "third"

    run(scenario())


def test_keyed_completed_entries_are_not_evicted():
    async def scenario():
        registry = OperationRegistry(1)
        first = await registry.run("cad", good, key="keep")
        assert (await registry.run("new", good))["error"]["code"] == "OPERATION_CAPACITY"
        replay = await registry.run("cad", good, key="keep")
        assert replay["replayed"] and replay["operation_id"] == first["operation_id"]

    run(scenario())


def test_cancelled_factory_is_indeterminate_and_never_evicted():
    async def scenario():
        async def factory():
            raise asyncio.CancelledError()

        registry = OperationRegistry(1)
        result = await registry.run("cad", factory)
        assert result["status"] == "indeterminate"
        assert result["error"]["code"] == "OPERATION_INDETERMINATE"
        assert (await registry.run("next", good))["error"]["code"] == "OPERATION_CAPACITY"
        assert (await registry.poll(result["operation_id"]))["status"] == "indeterminate"

    run(scenario())


@pytest.mark.parametrize("wait", [-1, 21, float("nan"), float("inf"), True, "1"])
def test_invalid_wait_is_rejected_without_scheduling(wait):
    async def scenario():
        registry = OperationRegistry()
        result = await registry.run("bad", good, wait_seconds=wait)
        assert result["error"]["code"] == "INVALID_ARGUMENT"
        assert not registry.list_recent()
        assert (await registry.poll("missing", wait_seconds=wait))["error"]["code"] == "INVALID_ARGUMENT"

    run(scenario())


def test_unknown_operation_and_invalid_arguments():
    async def scenario():
        registry = OperationRegistry()
        for identifier in ("missing", [], None):
            assert (await registry.poll(identifier))["error"]["code"] == "OPERATION_NOT_FOUND"
        for name in ("", " " * 2, "x" * 201, None):
            assert not (await registry.run(name, good))["ok"]
        for key in ("", "x" * 257, 1):
            assert not (await registry.run("good", good, key=key))["ok"]
        assert not (await registry.run("good", None))["ok"]
        assert not registry.list_recent()

    run(scenario())
    for maximum in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            OperationRegistry(maximum)


def test_drain_waits_for_running_work_before_shutdown():
    async def scenario():
        gate = asyncio.Event()

        async def factory():
            await gate.wait()
            return {"ok": True}

        registry = OperationRegistry()
        result = await registry.run("cad", factory, wait_seconds=0)
        draining = asyncio.create_task(registry.drain())
        await asyncio.sleep(0)
        assert not draining.done()
        gate.set()
        await draining
        assert (await registry.poll(result["operation_id"]))["status"] == "completed"
        await registry.drain()  # no pending operations is immediately safe

    run(scenario())


def test_cancelled_drain_does_not_cancel_operation_tasks():
    async def scenario():
        gate = asyncio.Event()
        calls = []

        async def factory():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
            calls.append("finished")
            return {"ok": True}

        registry = OperationRegistry()
        result = await registry.run("render", factory, wait_seconds=0)
        draining = asyncio.create_task(registry.drain())
        await asyncio.sleep(0)
        draining.cancel()
        with pytest.raises(asyncio.CancelledError):
            await draining
        assert (await registry.poll(result["operation_id"]))["status"] == "running"
        assert not calls
        gate.set()
        await registry.drain()
        assert calls == ["finished"]

    run(scenario())
