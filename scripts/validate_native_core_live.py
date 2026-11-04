"""Exercise independent CAD handlers on isolated, newly created native fixtures.

Requires --live and an initially empty running SOLIDWORKS session. Cases never
adopt existing user documents. Full failure evidence is retained, including
blocked prerequisites and unverified cleanup; a registered handler is not
counted as native success merely because its mocked tests passed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
import time
from uuid import uuid4

from solidworks_mcp_pro import __version__
from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro.native.catalogue import validate_coverage
from solidworks_mcp_pro.server import ProApplication


def path_value(value, path):
    for token in path.split(".") if path else []:
        value = value[int(token)] if isinstance(value, (list, tuple)) else value[token]
    return value


def expand(value, variables, output):
    if isinstance(value, dict):
        return {key: expand(item, variables, output) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, variables, output) for item in value]
    if isinstance(value, str) and value.startswith("$output/"):
        result = (output / value[len("$output/"):]).resolve()
        if not result.is_relative_to(output.resolve()):
            raise ValueError("Fixture output escaped its case directory")
        return str(result)
    if isinstance(value, str) and value.startswith("$"):
        key, _, path = value[1:].partition(".")
        return path_value(variables[key], path)
    return value


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and subset(value, actual[key]) for key, value in expected.items())
    if type(expected) in (int, float) and type(actual) in (int, float):
        return math.isclose(expected, actual, rel_tol=1e-7, abs_tol=1e-5)
    return actual == expected


BOX = {"tool": "add_box", "args": {"width_mm": 80, "height_mm": 50, "depth_mm": 10, "name": "Base"}, "bind": "base"}
FOUNDATION = [
    {"id": "get_status", "args": {}, "check": {"result": {"connected": True}}},
    {"id": "new_part", "args": {}, "check": {"result": {"type": 1, "created": True}}},
    {"id": "list_documents", "args": {}, "check": {"result": {"count": 1}}},
    {"id": "activate_document", "args": {"title": "$fresh.title", "allow_edit": True}},
    {"id": "rebuild", "setup": [BOX], "args": {}, "check": {"volume_mm3": 40000}},
    {"id": "save_part", "setup": [BOX], "args": {"path": "$output/source.sldprt"}},
    {"id": "open_part", "setup": [BOX, {"tool": "save_part", "args": {"path": "$output/source.sldprt"}},
        {"tool": "close_part", "args": {}}], "args": {"path": "$output/source.sldprt"}, "check": {"volume_mm3": 40000}},
    {"id": "close_part", "setup": [BOX], "args": {"save": False}},
    {"id": "new_assembly", "args": {}, "check": {"result": {"type": 2, "created": True}}},
    {"id": "save_assembly", "setup": [{"tool": "new_assembly", "args": {}}], "args": {"path": "$output/source.sldasm"}},
    {"id": "open_assembly", "setup": [{"tool": "new_assembly", "args": {}},
        {"tool": "save_assembly", "args": {"path": "$output/source.sldasm"}}, {"tool": "close_part", "args": {}}],
        "args": {"path": "$output/source.sldasm"}},
    {"id": "export", "setup": [BOX], "args": {"path": "$output/part.step"}},
    {"id": "screenshot", "setup": [BOX], "args": {"path": "$output/part.png"}},
    {"id": "make_drawing", "setup": [BOX, {"tool": "save_part", "args": {"path": "$output/source.sldprt"}}],
        "args": {"path": "$output/part.pdf"}},
]


def cases_for(group):
    groups = ("foundation", "geometry", "features", "assembly") if group == "all" else (group,)
    cases = []
    for name in groups:
        values = FOUNDATION if name == "foundation" else importlib.import_module("native_cases_" + name).CASES
        cases.extend([{**value, "group": name, "tool": value.get("tool", value["id"])} for value in values])
    if len({value["id"] for value in cases}) != len(cases):
        raise ValueError("Native fixture IDs must be unique")
    return cases


async def run(output, *, group="all", only=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    implementations = validate_coverage()
    cases = cases_for(group)
    if only:
        selected = set(only)
        cases = [case for case in cases if case["id"] in selected]
        if {case["id"] for case in cases} != selected:
            raise ValueError("Unknown --only case")
    records = []
    report = {"version": __version__, "kind": "independent_native_acceptance", "ok": False,
        "registered_implementations": len(implementations), "cases": records,
        "limits": ["Only explicit fixture inputs and this installed SDK/application were tested.",
                   "Mocks, registration coverage and real native acceptance are separate evidence.",
                   "The runner closes only documents created/opened by its own session."]}
    sources = {str(Path(importlib.import_module(fn.__module__).__file__).resolve()) for fn in implementations.values()}
    # Binding, worker and transaction policy are part of the tested engine too.
    native_directory = Path(importlib.import_module("solidworks_mcp_pro.native").__file__).resolve().parent
    sources.update(str(path) for path in native_directory.glob("*.py"))
    sources.add(str(native_directory / "contracts.json"))
    sources.update(str(path.resolve()) for path in Path(__file__).resolve().parent.glob("native_cases_*.py"))
    sources.add(str(Path(__file__).resolve()))
    report["implementation_hashes"] = {str(Path(p).relative_to(Path(__file__).resolve().parents[1])):
        hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(sources)}
    backend = NativeBackend()
    app = ProApplication(backend, output)
    await app.initialize()

    async def call(tool, args=None):
        response = await app.call(tool, args or {})
        value = response.structuredContent
        while value.get("operation_status") == "running":
            response = await app.call("pro_operation", {"operation_id": value["operation_id"],
                "wait_seconds": 20, "result_view": "full", "include_images": False})
            value = response.structuredContent
        return value

    async def close_owned():
        def cleanup():
            session = backend.runtime._session
            if session._sw is None:
                return {"verified": True, "closed": [], "foreign": []}
            closed, foreign = [], []
            # Closing an assembly can also retire its component-document COM
            # handles. Enumerate again after each close; never reuse that list.
            for _ in range(1000):
                documents = sorted(session._open_documents(), key=lambda doc: int(doc.GetType()), reverse=True)
                owned = [doc for doc in documents if session.rights(doc)["opened"]]
                if not owned:
                    foreign = [str(doc.GetTitle()) for doc in documents]
                    break
                document = owned[0]
                title = str(document.GetTitle())
                session.forget(document)
                session._sw.CloseDoc(title)
                closed.append(title)
                if any(str(doc.GetTitle()) == title for doc in session._open_documents()):
                    raise RuntimeError("Native fixture document remained open: " + title)
            else:
                raise RuntimeError("Native fixture cleanup exceeded its document bound")
            session._model = None
            session._documents_rights.clear()
            remaining = session._open_documents()
            return {"verified": not remaining, "closed": closed, "foreign": foreign,
                    "remaining": [str(document.GetTitle()) for document in remaining]}
        return await app._locked(lambda: backend._workflow_native(cleanup))

    try:
        report["connection"] = await call("get_status")
        if report["connection"].get("connected") is not True:
            raise RuntimeError("Start SOLIDWORKS first; acceptance does not launch a second instance")
        documents = await call("list_documents")
        if documents.get("count") != 0 or documents.get("documents") != []:
            raise RuntimeError("An empty session is required; existing documents were preserved")
        for case in cases:
            directory = output / case["id"]
            directory.mkdir()
            item = {"id": case["id"], "tool": case["tool"], "group": case["group"],
                    "status": "blocked", "steps": [], "target_exercised": False}
            records.append(item)
            start = time.perf_counter()
            try:
                fresh = await call("new_part")
                item["steps"].append({"tool": "new_part", "result": fresh})
                if fresh.get("ok") is not True:
                    raise RuntimeError("Fresh-part creation failed")
                variables = {"fresh": fresh}
                for step in case.get("setup", []):
                    args = expand(step.get("args", {}), variables, directory)
                    result = await call(step["tool"], args)
                    item["steps"].append({"tool": step["tool"], "arguments": args, "result": result})
                    if result.get("ok") is not True:
                        raise RuntimeError("Fixture setup failed at " + step["tool"])
                    if step.get("bind"):
                        variables[step["bind"]] = result
                args = expand(case.get("args", {}), variables, directory)
                item["target_exercised"] = True
                item["status"] = "failed"
                result = await call(case["tool"], args)
                item.update(arguments=args, result=result)
                if result.get("ok") is not True:
                    raise RuntimeError("Native target operation failed")
                check = expand(case.get("check", {}), variables, directory)
                if "result" in check and not subset(check["result"], result):
                    raise RuntimeError("Native result did not meet its explicit subset requirement")
                for path, expected in check.get("path_equals", {}).items():
                    if not subset(expected, path_value(result, path)):
                        raise RuntimeError("Result requirement failed: " + path)
                for path, requirement in check.get("path_close", {}).items():
                    actual = path_value(result, path)
                    item.setdefault("numeric_checks", []).append({"path": path, "actual": actual, **requirement})
                    if (type(actual) not in (int, float) or not math.isclose(actual, requirement["expected"],
                            rel_tol=requirement.get("rel_tolerance", 0), abs_tol=requirement["abs_tolerance"])):
                        raise RuntimeError("Explicit numeric tolerance requirement failed: " + path)
                if "volume_mm3" in check:
                    measured = await call("get_mass_properties")
                    item["independent_volume_check"] = {"expected_mm3": check["volume_mm3"], "native": measured}
                    actual = measured.get("mass_properties", {}).get("volume_mm3")
                    if (measured.get("ok") is not True or type(actual) not in (int, float)
                            or not math.isclose(actual, check["volume_mm3"], rel_tol=1e-7,
                                                abs_tol=check.get("abs_tolerance", 1e-5))):
                        raise RuntimeError("Analytic volume requirement failed")
                if "body_count" in check:
                    bodies = await call("list_bodies")
                    item["body_check"] = bodies
                    if bodies.get("ok") is not True or bodies.get("count") != check["body_count"]:
                        raise RuntimeError("Native solid-body count requirement failed")
                item["status"] = "passed"
            except Exception as exc:
                item["error"] = str(exc)
            finally:
                await app.registry.drain()
                try:
                    item["cleanup"] = await close_owned()
                except Exception as exc:
                    item["cleanup"] = {"verified": False, "error": str(exc)}
                item["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
                (directory / "evidence.json").write_text(json.dumps(item, indent=2, ensure_ascii=True), encoding="utf-8")
                print(json.dumps({"case": item["id"], "status": item["status"], "error": item.get("error"),
                    "code": item.get("result", {}).get("code"), "cleanup": item["cleanup"]["verified"]}), flush=True)
                (output / "evidence.json").write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
                if not item["cleanup"]["verified"]:
                    raise RuntimeError("Fixture cleanup was not verified; refusing further native work")
        report["ok"] = all(item["status"] == "passed" for item in records)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        await app.registry.drain()
        backend.close()
        report["counts"] = {"planned": len(cases), "completed": len(records),
            "passed": sum(item["status"] == "passed" for item in records),
            "failed": sum(item["status"] == "failed" for item in records),
            "blocked": sum(item["status"] == "blocked" for item in records)}
        report["external_cad_engine_imported"] = any(
            name == "solidworks_mcp" or name.startswith("solidworks_mcp.") for name in sys.modules)
        if report["external_cad_engine_imported"]:
            report["ok"] = False
        (output / "evidence.json").write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--group", choices=("all", "foundation", "geometry", "features", "assembly"), default="all")
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for native fixture creation")
    output = args.output or Path(__file__).resolve().parents[1] / "artifacts" / "independent-core" / uuid4().hex[:12]
    result = asyncio.run(run(output, group=args.group, only=args.only))
    print(json.dumps({key: result.get(key) for key in ("ok", "counts", "error", "external_cad_engine_imported")}))
    print(str(output.resolve() / "evidence.json"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
