"""Opt-in live failure recovery on a fresh, benchmark-owned part only."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.oracle import connection, snapshot
from benchmarks.run_live import Driver, default_specs
from benchmarks.run_protocol import connect, sanitize_report


def ui_state(title):
    snapshot(title)
    with connection() as (app, module, binding):
        model = binding.wrap(app.ActiveDoc, module.IModelDoc2)
        view = binding.wrap(model.ActiveView, module.IModelView)
        features = binding.wrap(model.FeatureManager, module.IFeatureManager)
        sketches = binding.wrap(model.SketchManager, module.ISketchManager)
        return {"command_in_progress": bool(app.CommandInProgress),
                "graphics_update": bool(view.EnableGraphicsUpdate),
                "feature_tree": bool(features.EnableFeatureTree),
                "feature_tree_window": bool(features.EnableFeatureTreeWindow),
                "display_when_added": bool(sketches.DisplayWhenAdded)}


async def run():
    root = Path(__file__).resolve().parents[1]
    folder = root / "artifacts/guard-acceptance" / uuid4().hex[:12]
    folder.mkdir(parents=True)
    protected = snapshot()["documents"]
    protected_titles = {document["title"] for document in protected}
    protected_ids = {(document["title"], document["path"]) for document in protected}
    report = {"ok": False, "protected_documents": protected}
    spec = next(s for s in default_specs(root) if s["id"] == "pro")
    driver = None
    try:
        async with connect(spec) as session:
            driver = Driver(session, "pro", folder)
            await driver.new()
            assert not any(d["title"] == driver.title for d in protected)
            await driver.block()
            before = await driver.call("list_features")
            initial = snapshot(driver.title)
            initial_ui = ui_state(driver.title)
            # A valid closed profile outside the body creates a sketch, then
            # native FeatureCut4 fails to intersect. The guard must remove it.
            failed = await driver.call("cut_profile", {
                "points_mm": [[1000, 1000], [1010, 1000], [1010, 1010], [1000, 1010]],
                "name": "ExpectedFailedCut"}, allow_error=True)
            after = await driver.call("list_features")
            final = snapshot(driver.title)
            final_ui = ui_state(driver.title)
            report.update(before=initial, after=final, ui_before=initial_ui, ui_after=final_ui,
                          features_before=before["output"], features_after=after["output"], failure=failed)
            assert failed["is_error"], "The outside-body cut should fail"
            assert "FeatureCut4 failed" in json.dumps(failed["output"]), "Not the intended partial-creation failure"
            assert before["output"]["features"] == after["output"]["features"], "Feature history changed"
            assert abs(initial["volume_mm3"] - final["volume_mm3"]) < 1e-6
            assert initial["size_mm"] == final["size_mm"]
            assert initial_ui == final_ui, "CAD/UI state was not restored"
            await driver.close()
            report["ok"] = True
    finally:
        if driver:
            report["calls"] = driver.calls
            remaining = snapshot()["documents"]
            owned = [d for d in remaining if d["title"] == driver.title
                     and d["title"] not in protected_titles
                     and (d["title"], d["path"]) not in protected_ids]
            if len(owned) == 1:
                with connection() as (app, _, _):
                    app.CloseDoc(driver.title)
                report["cleanup"] = "Only the newly created test document was closed"
        report["remaining_documents"] = snapshot()["documents"]
        output = folder / "evidence.json"
        output.write_text(json.dumps(sanitize_report(report), indent=2), encoding="utf-8")
        print(output, flush=True)
    print("Live failed-feature rollback, mass/bounds and UI restoration: PASS", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(run())
