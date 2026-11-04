"""Opt-in v0.6 acceptance on a newly created, independently measured part.

Requires an empty running CAD session. Never edits or closes existing documents.
Evidence stays in a new output directory, including failures and exact cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
from uuid import uuid4

from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro.server import ProApplication


async def run(output: Path, *, render: bool = False, render_route: str = "addin") -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    backend = NativeBackend()
    app = ProApplication(backend, output)
    records = []
    evidence = {"kind": "v0.6-native-engineering-acceptance", "ok": False,
                "render_requested": render, "render_route": render_route, "records": records, "cleanup": {},
                "limitations": ["One isolated plate, not arbitrary-model acceptance.",
                                "Variant ranking does not establish strength or manufacturability.",
                                "Render dimensions/hash do not certify composition or material fidelity."]}
    owned = None
    owned_documents = []
    source = output / "EngineeringPlate.sldprt"

    async def call(name, args=None):
        response = await app.call(name, args or {})
        data = response.structuredContent
        while data.get("operation_status") == "running":
            response = await app.call("pro_operation", {"operation_id": data["operation_id"],
                "wait_seconds": 20, "result_view": "full", "include_images": False})
            data = response.structuredContent
        records.append({"tool": name, "result": data})
        print(json.dumps({"tool": name, "ok": data.get("ok"), "status": data.get("status"),
                          "code": data.get("code")}), flush=True)
        if response.isError:
            raise RuntimeError(json.dumps(data, ensure_ascii=True))
        return data

    try:
        await app.initialize()
        doctor = await call("pro_connection_doctor")
        if doctor["status"] != "ready":
            raise RuntimeError("Align the installed package and source before native acceptance.")
        await call("get_status")
        inventory = await call("list_documents")
        if inventory.get("count") != 0 or inventory.get("documents") != []:
            raise RuntimeError("An empty running SOLIDWORKS session is required; existing files were preserved.")
        await call("new_part")
        owned = await backend._workflow_native(lambda: backend.upstream._session._model)
        owned_documents.append(owned)
        box = await call("add_box", {"width_mm": 100, "height_mm": 70, "depth_mm": 10})
        for x, y in ((15, 15), (85, 15), (15, 55), (85, 55)):
            await call("add_hole", {"diameter_mm": 8, "x_mm": x, "y_mm": y})
        await call("add_hole", {"diameter_mm": 24, "x_mm": 50, "y_mm": 35})
        await call("save_part", {"path": str(source)})
        dimension = box["depth_dimension"]
        expected_volume = (100 * 70 - 4 * math.pi * 4**2 - math.pi * 12**2) * 10
        checks = [
            {"id": "thickness", "kind": "dimension", "name": dimension, "unit": "mm",
             "expected": 10, "abs_tolerance": 1e-6},
            {"id": "analytic-volume", "kind": "volume", "unit": "mm3",
             "expected": expected_volume, "abs_tolerance": 0.001},
            {"id": "z-size", "kind": "bbox_size", "axis": "z", "unit": "mm",
             "expected": 10, "abs_tolerance": 1e-6},
            {"id": "native-mass", "kind": "mass", "unit": "kg", "min": 0,
             "abs_tolerance": 0},
        ]
        validated = await call("pro_validate_design", {"document_path": str(source),
            "assertions": checks, "result_view": "full"})
        if not validated["passed"] or not validated.get("state_unchanged"):
            raise RuntimeError("Native design requirements or read-only state verification failed.")
        failed = await call("pro_validate_design", {"document_path": str(source), "assertions": [
            {**checks[0], "expected": 999}], "result_view": "full"})
        if failed["passed"] or failed["counts"]["failed"] != 1:
            raise RuntimeError("An intentionally wrong design requirement was not rejected.")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        arguments = {"document_path": str(source), "request_id": "plate-variants",
                     "parameters": [{"dimension": dimension, "unit": "mm", "values": [6, 8, 10]}],
                     "assertions": [{"id": "volume-range", "kind": "volume", "unit": "mm3",
                                      "min": expected_volume * 0.59, "max": expected_volume * 1.01,
                                      "abs_tolerance": 0.001}], "result_view": "full"}
        planned = await call("pro_variant_study", {**arguments, "dry_run": True})
        if not planned["dry_run"] or hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("Variant planning changed source bytes.")
        study = await call("pro_variant_study", {**arguments, "dry_run": False})
        if not study.get("source_unchanged") or not study.get("active_document_restored"):
            raise RuntimeError("Variant source or active-document restoration was not verified.")
        if len(study.get("ranked_variants", [])) != 3:
            raise RuntimeError("Expected three valid independently generated plate variants.")
        replay = await call("pro_variant_study", {**arguments, "dry_run": False})
        if replay.get("operation_id") != study.get("operation_id"):
            raise RuntimeError("Variant request replay did not reuse its original operation.")
        restored = await call("pro_validate_design", {"document_path": str(source),
            "assertions": checks, "result_view": "full"})
        if not restored["passed"] or hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("The original geometry or source file changed during the study.")
        if render:
            await call("pro_visualize_status", {"load_if_missing": True})
            pack = await call("pro_shot_pack", {"document_path": str(source), "request_id": "plate-shots",
                "render_route": render_route,
                "shots": [{"name": "hero", "view": "isometric", "width": 512, "height": 512},
                          {"name": "front", "view": "front", "width": 640, "height": 360},
                          {"name": "portrait", "view": "custom", "from_direction": [1, 0.55, 1.4],
                           "width": 360, "height": 640}],
                "passes": 16, "denoiser": True, "result_view": "full"})
            if not pack.get("cleanup", {}).get("camera", {}).get("verified"):
                raise RuntimeError("Shot-pack camera restoration was not verified.")
            evidence["shot_pack"] = pack
        # A second independent check exercises assembly transforms and exact
        # component-pair distance, without relying on viewport measurements.
        await call("new_assembly")
        assembly_doc = await backend._workflow_native(lambda: backend.upstream._session._model)
        owned_documents.append(assembly_doc)
        component_names = []
        for x in (0, 140):
            inserted = await call("insert_component", {"path": str(source), "x_mm": x,
                                                        "y_mm": 0, "z_mm": 0, "fixed": False})
            entry = inserted.get("component", inserted)
            component_names.append(entry["name"])
        assembly_path = output / "EngineeringAssembly.sldasm"
        await call("save_assembly", {"path": str(assembly_path)})
        assembly_report = await call("pro_validate_design", {"document_path": str(assembly_path),
            "result_view": "full", "assertions": [
                {"id": "assembly-width", "kind": "bbox_size", "axis": "x", "unit": "mm",
                 "expected": 240, "abs_tolerance": 1e-5},
                {"id": "clearance", "kind": "distance", "component_a": component_names[0],
                 "component_b": component_names[1], "unit": "mm", "expected": 40, "abs_tolerance": 1e-5},
                {"id": "assembly-mass", "kind": "mass", "unit": "kg",
                 "expected": validated["measurements"]["mass_kg"] * 2, "abs_tolerance": 1e-7},
            ]})
        if not assembly_report["passed"]:
            raise RuntimeError("Native assembly bounds, distance or mass failed independent checks.")
        evidence["assembly_validation"] = assembly_report
        evidence.update(ok=True, status="passed", fixture_path=str(source),
                        expected_volume_mm3=expected_volume, source_sha256=source_hash)
    except Exception as exc:
        evidence.update(ok=False, status="failed", error=str(exc))
    finally:
        await app.registry.drain()
        if owned_documents:
            def close_owned():
                session = backend.upstream._session
                closed, errors = [], []
                for document in reversed(owned_documents):
                    current = [doc for doc in session._open_documents()
                               if getattr(doc, "_oleobj_", doc) == getattr(document, "_oleobj_", document)]
                    if len(current) != 1:
                        errors.append("Fixture ownership could not be re-established.")
                        continue
                    title = current[0].GetTitle()
                    session._sw.CloseDoc(title)
                    if any(getattr(doc, "_oleobj_", doc) == getattr(document, "_oleobj_", document)
                           for doc in session._open_documents()):
                        errors.append("Owned fixture did not close: " + title)
                    else:
                        closed.append(title)
                session._model = None
                return {"ok": not errors, "verified": not errors, "closed": closed, "errors": errors}
            try:
                evidence["cleanup"] = await app._locked(lambda: backend._workflow_native(close_owned))
                if not evidence["cleanup"].get("verified"):
                    evidence.update(ok=False, status="indeterminate")
            except Exception as exc:
                evidence.update(ok=False, status="indeterminate")
                evidence["cleanup"] = {"verified": False, "error": str(exc)}
        backend.close()
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=True), encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--render-route", choices=("addin", "project"), default="addin")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this creates new native scratch geometry")
    output = args.output or Path(__file__).resolve().parents[1] / "artifacts" / "engineering-live" / uuid4().hex[:12]
    result = asyncio.run(run(output, render=args.render, render_route=args.render_route))
    print(json.dumps({key: result.get(key) for key in ("ok", "status", "error", "cleanup")}, ensure_ascii=True))
    print(str(output.resolve() / "evidence.json"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
