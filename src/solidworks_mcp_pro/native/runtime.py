"""Independent dispatch runtime; no external CAD MCP package is imported."""
from __future__ import annotations

import asyncio
from importlib import resources

from .catalogue import list_tools, validate_coverage
from .errors import CadError
from .session import NativeSession
from .worker import StaWorker


INSTRUCTIONS = (
    "Independent SolidWorks MCP PRO native engine. Public units are millimetres, degrees and kilograms. "
    "The default box profile is on Front (XY), extruded along +Z; SOLIDWORKS world up is +Y. "
    "Call get_status to attach to running CAD. Create new documents for modeling. "
    "Existing documents require explicit allow_edit=true on open_part/open_assembly/activate_document. "
    "Tools use the exact active target and never silently retarget another document. "
    "Read native failures, postconditions, rollback scope and measurements. Do not repeat uncertain operations. "
    "Entity IDs expire after model/configuration changes. Read solidworks://guide."
)


def failure(exc):
    if isinstance(exc, CadError):
        return exc.result()
    return {"ok": False, "code": "NATIVE_API_ERROR", "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}


class _Dispatch:
    def __init__(self, runtime):
        self.runtime = runtime

    async def list_tools(self):
        return list_tools()

    async def call_tool(self, name, arguments):
        future = self.runtime._worker.submit(lambda: self.runtime._session.invoke(name, arguments))
        return await asyncio.shield(asyncio.wrap_future(future))


class IndependentRuntime:
    INSTRUCTIONS = INSTRUCTIONS
    _failure = staticmethod(failure)

    def __init__(self):
        validate_coverage()
        self._worker = StaWorker(NativeSession)
        self._session = self._worker.session
        self.mcp = _Dispatch(self)

    @staticmethod
    def guide():
        return resources.files(__package__).joinpath("guide.md").read_text(encoding="utf-8")

