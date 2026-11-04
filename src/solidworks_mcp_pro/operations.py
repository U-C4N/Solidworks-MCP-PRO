"""Background operations that outlive an individual MCP request's wait.

Only the server process owns these tasks. A request timeout or cancellation
does not cancel its CAD/render call. Process termination is outside this
guarantee; callers must inspect SolidWorks before resubmitting uncertain work.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time
from typing import Awaitable, Callable
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(code: str, message: str, *, status: str = "rejected", **extra) -> dict:
    return {"ok": False, "status": status, "error": {"code": code, "message": message}, **extra}


@dataclass
class _Operation:
    operation_id: str
    name: str
    key: str | None
    started_at: str
    started_monotonic: float
    status: str = "running"
    finished_at: str | None = None
    duration_ms: float | None = None
    result: dict | None = None
    task: asyncio.Task | None = None

    def metadata(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
        }


class OperationRegistry:
    """Retain strong task references and poll results without automatic retries.

    Capacity includes active and finished operations. At capacity only the
    oldest finished, unkeyed operation can be evicted. Running, indeterminate
    and keyed operations are never evicted: losing a key would allow a retry
    to repeat a mutation. If no entry can be evicted, new work is rejected.

    The same key and name reuses its task/result. The caller must separately
    validate that a reused key refers to the same payload (WorkflowEngine does
    this for plans). Registry state and key protection last for this process.
    """

    def __init__(self, max_operations: int = 256) -> None:
        if type(max_operations) is not int or max_operations < 1:
            raise ValueError("max_operations must be a positive integer")
        self.max_operations = max_operations
        self._operations: dict[str, _Operation] = {}
        self._keys: dict[str, str] = {}

    @staticmethod
    def _validate_wait(wait_seconds: float) -> None:
        if (
            type(wait_seconds) not in (int, float)
            or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= 20
        ):
            raise ValueError("wait_seconds must be finite and between 0 and 20")

    def _make_room(self) -> bool:
        if len(self._operations) < self.max_operations:
            return True
        for operation_id, operation in self._operations.items():
            if operation.key is None and operation.status in {"completed", "failed"}:
                del self._operations[operation_id]
                return True
        return False

    async def _perform(self, operation: _Operation, factory: Callable[[], Awaitable[dict]]) -> dict:
        try:
            result = await factory()
            if type(result) is not dict:
                raise TypeError("Operation factory must return a dictionary")
            # Convert to a detached JSON value; reject nonfinite or nonserializable results.
            result = json.loads(json.dumps(result, allow_nan=False, ensure_ascii=True))
            if result.get("status") == "indeterminate":
                operation.status = "indeterminate"
            elif result.get("ok") is False or result.get("isError") is True:
                operation.status = "failed"
            else:
                operation.status = "completed"
            operation.result = result
        except asyncio.CancelledError:
            # Only explicit task cancellation/process shutdown can reach here.
            # A cancelled CAD await does not prove the native call stopped.
            operation.status = "indeterminate"
            operation.result = _error(
                "OPERATION_INDETERMINATE",
                "The operation task was cancelled. CAD/rendering may have completed; inspect before retrying.",
                status="indeterminate",
            )
        except Exception as exc:
            operation.status = "failed"
            operation.result = _error(
                "OPERATION_FAILED", f"{type(exc).__name__}: {str(exc)[:500]}", status="failed",
                outcome_may_be_indeterminate=True,
            )
        finally:
            operation.finished_at = _now()
            operation.duration_ms = round((time.monotonic() - operation.started_monotonic) * 1000, 3)
        return self._result(operation)

    @staticmethod
    def _result(operation: _Operation) -> dict:
        if operation.status == "running":
            return {
                "ok": True, "status": "running", "operation_status": "running",
                "operation_id": operation.operation_id, "name": operation.name,
                "started_at": operation.started_at,
                "message": "Operation continues in the server; poll this operation_id. Do not resubmit the CAD call.",
            }
        result = deepcopy(operation.result or {})
        result["operation_id"] = operation.operation_id
        result["operation_status"] = operation.status
        result.setdefault("status", operation.status)
        return result

    async def _wait(self, operation: _Operation, wait_seconds: float) -> dict:
        task = operation.task
        if task is None:
            raise RuntimeError("Operation was not scheduled")
        if not task.done():
            # asyncio.wait never cancels its input tasks, including when this
            # waiter is cancelled. Unlike wait_for it has no timeout side effect.
            await asyncio.wait({task}, timeout=wait_seconds)
        if task.done():
            # Shield also preserves ownership if cancellation arrives while
            # retrieving a completed task's result.
            await asyncio.shield(task)
        return self._result(operation)

    async def run(
        self,
        name: str,
        factory: Callable[[], Awaitable[dict]],
        *,
        wait_seconds: float = 20,
        key: str | None = None,
    ) -> dict:
        """Start once, returning its result or a handle within at most 20 seconds."""
        try:
            self._validate_wait(wait_seconds)
            if type(name) is not str or not name.strip() or len(name) > 200:
                raise ValueError("name must contain 1..200 characters")
            if key is not None and (type(key) is not str or not key or len(key) > 256):
                raise ValueError("key must be a nonempty string of at most 256 characters")
            if not callable(factory):
                raise ValueError("factory must be callable")
        except ValueError as exc:
            return _error("INVALID_ARGUMENT", str(exc))

        if key is not None and key in self._keys:
            operation = self._operations[self._keys[key]]
            if operation.name != name:
                return _error("OPERATION_KEY_CONFLICT", "This key already belongs to a different operation name")
            result = await self._wait(operation, wait_seconds)
            result["replayed"] = True
            return result

        if not self._make_room():
            return _error(
                "OPERATION_CAPACITY", "Registry is full of running, indeterminate or keyed operations; no new work was started.",
            )

        operation_id = "op_" + uuid4().hex
        operation = _Operation(operation_id, name, key, _now(), time.monotonic())
        self._operations[operation_id] = operation
        if key is not None:
            self._keys[key] = operation_id
        operation.task = asyncio.create_task(self._perform(operation, factory), name=f"solidworks:{operation_id}")
        return await self._wait(operation, wait_seconds)

    async def poll(self, operation_id: str, wait_seconds: float = 0) -> dict:
        """Read/wait for an existing operation without submitting another call."""
        try:
            self._validate_wait(wait_seconds)
        except ValueError as exc:
            return _error("INVALID_ARGUMENT", str(exc))
        if type(operation_id) is not str or operation_id not in self._operations:
            return _error("OPERATION_NOT_FOUND", "Unknown or expired operation_id")
        return await self._wait(self._operations[operation_id], wait_seconds)

    def list_recent(self) -> list[dict]:
        """Metadata only, newest first; results are available via poll."""
        return [operation.metadata() for operation in reversed(list(self._operations.values()))]

    async def drain(self) -> None:
        """Wait for unfinished work before normal server shutdown.

        Cancelling the draining caller propagates cancellation to that caller
        only: the registry's operation tasks continue and another drain/poll
        can join them. Stop accepting MCP requests before draining; operations
        added during a wait are picked up on the next pass.
        """
        while pending := [
            operation.task for operation in self._operations.values()
            if operation.task is not None and not operation.task.done()
        ]:
            await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True)
