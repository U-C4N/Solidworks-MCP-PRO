"""Opt-in native acceptance on newly created scratch documents only.

Creates a part and a small assembly; verifies diagnosis/repair, RGB and pose
restoration. --render also runs preview/final Visualize jobs. Native evidence
is separate from the non-native envelope benchmark. No existing file is edited.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys
import time
from uuid import uuid4

from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro.server import ProApplication
from solidworks_mcp_pro.storage import CadLease


async def run(output: Path, *, render: bool = False, repeats: int = 3, allow_render_unavailable: bool = False) -> dict:
    if output.exists():
        raise ValueError("Choose a new output directory")
    output.mkdir(parents=True)
    backend = NativeBackend()
    app = ProApplication(backend, output)
    await app.initialize()
    records = []
    evidence = {"kind": "native_scratch_acceptance", "render_requested": render,
                "records": records, "limitations": [
                    "Small scratch fixture; no universal latency or quality claim.",
                    "Timed calls exclude fixture setup and explicit reset operations.",
                    "Render presets are not compared as equal-quality workloads."]}

    async def call(name, arguments=None, *, allow_error=False):
        arguments = arguments or {}
        start = time.perf_counter()
        response = await app.call(name, arguments)
        data = response.structuredContent
        while data.get("operation_status") == "running":
            response = await app.call("pro_operation", {
                "operation_id": data["operation_id"], "wait_seconds": 20,
                "result_view": "full", "include_images": False})
            data = response.structuredContent
        record = {"tool": name, "arguments": arguments, "result": data,
                  "duration_ms": round((time.perf_counter() - start) * 1000, 3)}
        records.append(record)
        print(json.dumps({"tool": name, "ok": data.get("ok"), "duration_ms": record["duration_ms"],
                          "code": data.get("code")}), flush=True)
        if response.isError and not allow_error:
            raise RuntimeError(json.dumps(data, ensure_ascii=True))
        return data

    try:
        await call("get_status")
        documents = await call("list_documents")
        if documents.get("count"):
            raise RuntimeError("Scratch validation requires an empty SOLIDWORKS session; no existing document was changed.")
        part = output / "FastFixture.sldprt"
        assembly = output / "FastAssembly.sldasm"
        await call("new_part")
        await call("add_box", {"width_mm": 40, "height_mm": 25, "depth_mm": 8})
        await call("save_part", {"path": str(part)})
        scan = await call("pro_diagnose", {"document_path": str(part), "deep": True, "result_view": "full"})
        if not scan["healthy"]:
            raise RuntimeError("Scratch part did not pass native health checks: " + json.dumps(scan))
        plan = await call("pro_repair", {"document_path": str(part), "result_view": "full"})
        assert plan["mutation_attempted"] is False and plan["requires_apply"]
        repaired = await call("pro_repair", {"action": "apply", "plan_id": plan["plan_id"], "result_view": "full"})
        assert repaired["ok"]
        await call("pro_colorize", {"document_path": str(part), "request_id": "part-color",
            "assignments": [{"color_rgb": [32, 100, 210]}], "result_view": "full"})
        await call("save_part", {"path": str(part), "overwrite": True})
        await call("new_assembly")
        components = []
        for index in range(4):
            inserted = await call("insert_component", {"path": str(part), "x_mm": index * 65,
                                                       "y_mm": 0, "z_mm": 0, "fixed": False})
            # Upstream versions can wrap placement under component.
            entry = inserted.get("component", inserted)
            components.append(entry["name"] if isinstance(entry, dict) else entry)
        await call("save_assembly", {"path": str(assembly)})
        scanned = await call("pro_diagnose", {"document_path": str(assembly), "deep": True, "result_view": "full"})
        assert scanned["healthy"], scanned
        poses = (await call("pro_assembly_pose", {"document_path": str(assembly), "transforms": [],
                                                   "dry_run": True}))["components"]
        assignments = [{"component": name, "color_rgb": [30 + i * 40, 80, 200 - i * 30]}
                       for i, name in enumerate(components)]
        reset = [{"component": name, "color_rgb": [32, 100, 210]} for name in components]
        measurements = {"legacy_per_component": [], "batch_rgb": []}
        for repetition in range(repeats):
            order = ["legacy_per_component", "batch_rgb"]
            if repetition % 2:
                order.reverse()
            for route in order:
                await call("pro_colorize", {"document_path": str(assembly), "assignments": reset,
                    "request_id": f"reset-{repetition}-{route}", "result_view": "full"})
                start = time.perf_counter()
                if route == "batch_rgb":
                    await call("pro_colorize", {"document_path": str(assembly), "assignments": assignments,
                        "request_id": f"batch-{repetition}", "result_view": "full"})
                else:
                    for assignment in assignments:
                        await call("set_appearance", {"component": assignment["component"], "rgb": assignment["color_rgb"]})
                measurements[route].append(round((time.perf_counter() - start) * 1000, 3))
        evidence["color_comparison"] = {route: {"samples_ms": values, "median_ms": statistics.median(values)}
                                        for route, values in measurements.items()}
        if render:
            await call("pro_visualize_status", {"load_if_missing": True})
            normal = await call("pro_render_job", {"document_path": str(assembly), "request_id": "normal",
                "assignments": assignments, "result_view": "full"}, allow_error=allow_render_unavailable)
            exploded = await call("pro_render_job", {"document_path": str(assembly), "request_id": "exploded",
                "mode": "exploded", "transforms": [{"component": item["component"],
                    "position_mm": [item["position_mm"][0], (index + 1) * 30, 0],
                    "rotation_deg": item["rotation_deg"]} for index, item in enumerate(poses)], "result_view": "full"},
                allow_error=allow_render_unavailable)
            for job in (normal, exploded):
                assert job["cleanup"]["camera"]["verified"]
                if job["ok"]:
                    assert len(job["preview_images"]) and len(job["images"])
                else:
                    assert allow_render_unavailable and job["code"] == "VISUALIZE_MACRO_RUN_FAILED", job
                    assert job["status"] == "failed" and job["failed_stage"] == "preview", job
            assert exploded["cleanup"]["poses"]["verified"]
            evidence["render_verified"] = normal["ok"] and exploded["ok"]
            evidence["render_validation"] = "success" if evidence["render_verified"] else "native_macro_unavailable_cleanup_verified"
        final_poses = (await call("pro_assembly_pose", {"document_path": str(assembly), "transforms": [],
                                                         "dry_run": True}))["components"]
        for old, new in zip(poses, final_poses):
            assert old["component"] == new["component"]
            assert all(abs(a - b) < 1e-9 for a, b in zip(old["transform"], new["transform"]))
        evidence["ok"] = True
    except Exception as exc:
        evidence.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await app.registry.drain()
        # Only close files created inside this unique scratch directory. No
        # active-document fallback and no blanket close-all are permitted.
        def close_owned():
            if backend.upstream._session._sw is None:
                return []
            from solidworks_mcp_pro.native import binding
            session = backend.upstream._session
            closed = []
            for raw in session._sw.GetDocuments() or ():
                document = binding.wrap(raw, session._mod.IModelDoc2)
                path = document.GetPathName()
                if path and Path(path).resolve().is_relative_to(output.resolve()):
                    title = document.GetTitle()
                    session._sw.CloseDoc(title)
                    closed.append(title)
            return closed
        try:
            with CadLease():
                evidence["closed_scratch_documents"] = await backend._workflow_native(close_owned)
        finally:
            (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=True, indent=2), encoding="utf-8")
            backend.close()
    return deepcopy(evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly create scratch native CAD fixtures")
    parser.add_argument("--render", action="store_true", help="also run real Visualize preview and final jobs")
    parser.add_argument("--allow-render-unavailable", action="store_true",
                        help="explicitly accept only known macro-run failure to test cleanup; never counts as render success")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; without it no CAD action is performed")
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats must be 1..20")
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / "artifacts" / "fast-live" / uuid4().hex[:12]
    asyncio.run(run(output.resolve(), render=args.render, repeats=args.repeats,
                    allow_render_unavailable=args.allow_render_unavailable))
    print(output / "evidence.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
