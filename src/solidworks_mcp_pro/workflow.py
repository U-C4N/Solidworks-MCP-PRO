"""Validated, sequential CAD plans with session-local replay protection.

This module never retries a CAD operation and never promises plan rollback.
A cancelled invocation can have changed SolidWorks; its request ID is therefore
retained as indeterminate until the process exits.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable

from jsonschema.validators import validator_for

from .validation import augment_schema, guard_native_write, normalize_args, validate_result

MAX_STEPS = 32
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_REQUESTS = 256
MAX_AUDIT_RECORDS = 256
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_CODE_TOOL = re.compile(
    r"(?:^|[^a-z0-9])(?:exec|execute|eval|shell|bash|powershell|python|vba|macro|script|"
    r"code|command|cmd|terminal|interpreter|javascript|subprocess)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def _json_bytes(value: Any) -> bytes:
    """Reject Python-only objects and nonfinite numbers rather than coercing."""
    def visit(item: Any, depth: int = 0) -> None:
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels")
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                visit(child, depth + 1)
            return
        raise ValueError(f"Unsupported JSON value: {type(item).__name__}")

    visit(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class WorkflowEngine:
    """Execute only prevalidated calls, recording outcomes without their arguments.

    ``tool_schemas`` may contain raw JSON schemas or MCP tool descriptors with
    ``inputSchema``. ``invoke`` returns a dict: ``ok=False`` or ``isError=True``
    stops the plan. All plans share ``execution_lock``; a caller can also use
    that lock for standalone native calls, but invoke must not reacquire it.

    Replay protection lasts for this engine/process only. Once 256 execution
    IDs are retained, new IDs are refused instead of evicting old evidence.
    """

    def __init__(
        self,
        tool_schemas: dict[str, dict],
        invoke: Callable[[str, dict], Awaitable[dict]],
        audit_path: Path | None = None,
        *,
        execution_scope: Callable[[], AbstractContextManager] | None = None,
    ) -> None:
        self._validators = {}
        for name, descriptor in tool_schemas.items():
            schema = augment_schema(name, descriptor.get("inputSchema", descriptor))
            cls = validator_for(schema)
            cls.check_schema(schema)
            self._validators[name] = cls(deepcopy(schema))
        self._invoke = invoke
        self.audit_path = Path(audit_path) if audit_path is not None else None
        self.execution_lock = asyncio.Lock()
        self._execution_scope = execution_scope or nullcontext
        self._requests: dict[str, tuple[str, dict]] = {}
        self._records: deque[dict] = deque(maxlen=MAX_AUDIT_RECORDS)

    @asynccontextmanager
    async def _execution_context(self):
        # Match standalone calls: queue within this server first, then acquire
        # the cross-process lease. Reversing these can reject our own queued plan.
        async with self.execution_lock:
            with self._execution_scope():
                yield

    def recent(self, limit: int = 20) -> list[dict]:
        """Return newest audit records in chronological order (1..256)."""
        if type(limit) is not int or not 1 <= limit <= MAX_AUDIT_RECORDS:
            raise ValueError("limit must be an integer between 1 and 256")
        return deepcopy(list(self._records)[-limit:])

    def _record(self, **fields: Any) -> str | None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        self._records.append(record)
        if self.audit_path is not None:
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(record, allow_nan=False, ensure_ascii=True) + "\n")
            except (OSError, ValueError) as exc:
                return f"Audit persistence failed: {type(exc).__name__}: {str(exc)[:300]}"
        return None

    def _validate(self, steps: list[dict], request_id: str, dry_run: bool) -> bytes:
        if type(request_id) is not str or not _REQUEST_ID.fullmatch(request_id):
            raise ValueError("request_id must be 1..128 letters, digits, dots, colons, underscores or hyphens")
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        if type(steps) is not list or not 1 <= len(steps) <= MAX_STEPS:
            raise ValueError("steps must contain between 1 and 32 calls")
        payload = _json_bytes(steps)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("Plan JSON exceeds 256 KiB")
        normalized_steps = []
        for index, step in enumerate(steps, start=1):
            if type(step) is not dict or set(step) != {"tool", "arguments"}:
                raise ValueError(f"Step {index} requires exactly tool and arguments")
            name, arguments = step["tool"], step["arguments"]
            if type(name) is not str or type(arguments) is not dict:
                raise ValueError(f"Step {index}: tool must be a string and arguments an object")
            if name.lower().startswith("pro_") or _CODE_TOOL.search(name):
                raise ValueError(f"Step {index}: recursive or code-execution tool is not permitted")
            if name not in self._validators:
                raise ValueError(f"Step {index}: unknown tool {name!r}")
            arguments = normalize_args(name, arguments)
            error = next(self._validators[name].iter_errors(arguments), None)
            if error is not None:
                location = "/".join(str(part) for part in error.absolute_path) or "arguments"
                raise ValueError(f"Step {index} ({name}), {location}: {error.message[:500]}")
            normalized_steps.append({"tool": name, "arguments": arguments})
        payload = _json_bytes(normalized_steps)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("Normalized plan JSON exceeds 256 KiB")
        return payload

    @staticmethod
    def _validate_outputs(steps: list[dict]) -> None:
        from .validation import FILE_WRITE_TOOLS

        targets = set()
        for step in steps:
            name, arguments = step["tool"], step["arguments"]
            guard_native_write(name, arguments)
            if name in FILE_WRITE_TOOLS:
                target = os.path.normcase(str(Path(arguments["path"]).resolve()))
                if target in targets and not arguments.get("overwrite", False):
                    raise ValueError("OUTPUT_REUSED: a later plan step would overwrite an earlier output; use distinct paths or overwrite=true")
                targets.add(target)

    async def execute(self, steps: list[dict], *, request_id: str, dry_run: bool = True) -> dict:
        """Validate all steps, then optionally execute in order and stop on failure.

        Failed/cancelled plans retain completed changes; inspect the document
        before issuing a new request ID. A dry run neither calls CAD nor
        reserves its request ID.
        """
        try:
            payload = self._validate(steps, request_id, dry_run)
        except (ValueError, TypeError, RecursionError) as exc:
            return {"ok": False, "status": "rejected", "error": str(exc), "executed_steps": 0}
        fingerprint = hashlib.sha256(payload).hexdigest()
        # Work from the validated snapshot even if the caller mutates its input.
        steps = json.loads(payload)
        warnings: list[str] = []

        def record(**fields: Any) -> None:
            warning = self._record(request_id=request_id, **fields)
            if warning and warning not in warnings:
                warnings.append(warning)

        def finish(result: dict) -> dict:
            if warnings:
                result["audit_warnings"] = warnings.copy()
            return result

        if dry_run:
            try:
                self._validate_outputs(steps)
            except (ValueError, OSError) as exc:
                return {"ok": False, "status": "rejected", "error": str(exc), "executed_steps": 0}
            record(event="plan_validated", status="validated", step_count=len(steps), fingerprint=fingerprint)
            return finish({
                "ok": True, "status": "validated", "request_id": request_id,
                "dry_run": True, "executed_steps": 0,
                "steps": [{"index": i, "tool": step["tool"]} for i, step in enumerate(steps, 1)],
                "rollback": "not_attempted", "idempotency_scope": "current_process",
            })

        async with self._execution_context():
            previous = self._requests.get(request_id)
            if previous is not None:
                previous_fingerprint, result = previous
                if previous_fingerprint != fingerprint:
                    return {
                        "ok": False, "status": "rejected", "request_id": request_id,
                        "error": "request_id was already used for a different plan", "executed_steps": 0,
                    }
                record(event="plan_replayed", status=result["status"])
                return finish({**deepcopy(result), "replayed": True})
            try:
                self._validate_outputs(steps)
            except (ValueError, OSError) as exc:
                return {"ok": False, "status": "rejected", "error": str(exc), "executed_steps": 0}
            if len(self._requests) >= MAX_REQUESTS:
                return {
                    "ok": False, "status": "rejected", "request_id": request_id,
                    "error": f"Session request limit reached ({MAX_REQUESTS}). Inspect/audit pending work before restarting.",
                    "executed_steps": 0,
                }

            result = {
                "ok": False, "status": "running", "request_id": request_id,
                "dry_run": False, "steps": [], "completed_steps": 0, "executed_steps": 0,
                "rollback": "not_attempted", "idempotency_scope": "current_process",
            }
            self._requests[request_id] = (fingerprint, result)
            record(event="plan_started", status="running", step_count=len(steps), fingerprint=fingerprint)
            for index, step in enumerate(steps, 1):
                name = step["tool"]
                started = time.monotonic()
                record(event="step_started", step=index, tool=name, status="running")
                result["executed_steps"] = index
                try:
                    response = await self._invoke(name, deepcopy(step["arguments"]))
                    if type(response) is not dict:
                        raise TypeError("Tool invocation must return a dict")
                    response = validate_result(response)
                    _json_bytes(response)
                except asyncio.CancelledError:
                    result.update(
                        status="indeterminate", failed_step=index,
                        error="Invocation cancelled; the CAD operation may have completed. Inspect the document; do not retry blindly.",
                    )
                    result["steps"].append({"index": index, "tool": name, "ok": False, "status": "indeterminate"})
                    record(event="step_finished", step=index, tool=name, status="indeterminate",
                           duration_ms=round((time.monotonic() - started) * 1000, 3))
                    finish(result)
                    raise
                except Exception as exc:
                    # The call may have changed CAD before raising; no retry.
                    response = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                                "outcome_may_be_indeterminate": True}

                success = response.get("ok") is not False and response.get("isError") is not True
                result["steps"].append({"index": index, "tool": name, "ok": success, "result": deepcopy(response)})
                record(event="step_finished", step=index, tool=name,
                       status="completed" if success else "failed",
                       duration_ms=round((time.monotonic() - started) * 1000, 3))
                if not success:
                    result.update(status="failed", failed_step=index,
                                  error="Plan stopped after a failed call; earlier completed changes remain.")
                    record(event="plan_finished", status="failed", completed_steps=result["completed_steps"])
                    return deepcopy(finish(result))
                result["completed_steps"] = index

            result.update(ok=True, status="completed")
            record(event="plan_finished", status="completed", completed_steps=len(steps))
            return deepcopy(finish(result))
