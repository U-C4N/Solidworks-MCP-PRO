"""Local evidence checks only; no renderer, CAD, or GPU calls."""
import json

from PIL import Image
import pytest

from benchmarks import analyze_render as renderer


def record(path, *, identifier="image", phase="preview", width=128, height=72, visual="not_run"):
    return {"id": identifier, "phase": phase, "status": "pass", "image_path": str(path),
            "settings": {"width": width, "height": height, "passes": 50, "denoiser": True},
            "timings_ms": {"client_wall": 150, "operation": 120}, "visual_review": {"status": visual}}


def test_local_decode_does_not_claim_quality_gpu_or_unmeasured_time(tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (128, 72), "red").save(image)
    report = renderer.analyze({"records": [record(image)]})
    item = report["records"][0]
    assert item["artifact_verified"] and not item["finished_4k"]
    assert item["visual_review"]["status"] == "not_run"
    assert item["image"]["constant_rgb_sample"]
    assert "unverified" in item["gpu_execution"]
    assert "native_render" not in report["groups"][0]["timings_ms"]


def test_actual_4k_requires_visual_pass_and_final_phase(tmp_path):
    image = tmp_path / "4k.png"
    Image.new("RGB", (3840, 2160), "grey").save(image)
    item = record(image, phase="final", width=3840, height=2160)
    assert not renderer.analyze({"records": [item]})["records"][0]["finished_4k"]
    item["visual_review"]["status"] = "pass"
    assert renderer.analyze({"records": [item]})["summary"]["finished_4k_ids"] == ["image"]


@pytest.mark.parametrize("mutation", ["size", "hash", "nan", "boolean", "missing", "visual_fail", "native_error"])
def test_bad_evidence_not_in_timing_groups(tmp_path, mutation):
    image = tmp_path / "image.png"
    Image.new("RGB", (128, 72), "red").save(image)
    item = record(image)
    if mutation == "size":
        item["settings"]["width"] = 100
    elif mutation == "hash":
        item["image_sha256"] = "0" * 64
    elif mutation == "nan":
        item["timings_ms"]["client_wall"] = float("nan")
    elif mutation == "boolean":
        item["timings_ms"]["client_wall"] = True
    elif mutation == "visual_fail":
        item["visual_review"]["status"] = "fail"
    elif mutation == "native_error":
        item["result"] = {"ok": False, "error": "Render failed"}
    else:
        image.unlink()
    report = renderer.analyze({"records": [item]})
    assert report["summary"]["fail_count"] == 1
    assert not report["groups"]


def test_warmup_and_changed_settings_are_not_pooled(tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (128, 72), "red").save(image)
    items = [record(image, identifier=str(i), phase="warmup" if i == 0 else "preview") for i in range(4)]
    items[3]["settings"]["denoiser"] = False
    items[0]["timings_ms"]["client_wall"] = 99999
    report = renderer.analyze({"records": items})
    assert len(report["groups"]) == 2
    assert report["groups"][0]["timings_ms"]["client_wall"]["samples"] == [150, 150]
    assert report["groups"][0]["record_ids"] == ["1", "2"]


def test_cli_raw_and_existing_outputs_are_preserved(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"records": []}))
    original = raw.read_bytes()
    with pytest.raises(SystemExit):
        renderer.main([str(raw), "--output", str(raw)])
    output = tmp_path / "analysis.json"
    output.write_text("preserve")
    with pytest.raises(SystemExit):
        renderer.main([str(raw), "--output", str(output)])
    assert raw.read_bytes() == original and output.read_text() == "preserve"


def test_duplicate_records_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        renderer.analyze({"records": [{"id": "same"}, {"id": "same"}]})
