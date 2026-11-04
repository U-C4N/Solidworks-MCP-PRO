"""Analyze captured render evidence and local images; never connect to CAD.

Input: {run_id, model, scene_id, environment, records:[{id, phase, status,
settings:{width,height,passes,denoiser}, timings_ms:{client_wall,operation,
native_render,import,verify}, image_path, visual_review:{status,notes}}]}.
Absent timing stages remain unmeasured. A decoded 4K image is not automatically
a quality-approved rendering; finished_4k also requires a recorded visual pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

from PIL import Image, ImageStat


TIMING_SCOPES = {
    "client_wall": "Client request through verified terminal response; includes polling and transport.",
    "operation": "Operation task duration; may include queue/lease wait, native work, verification and registration.",
    "native_render": "Wall time around native Render(); may include preparation/import and file output; not GPU execution time.",
    "import": "Explicitly instrumented import stage only.",
    "verify": "Explicitly instrumented output verification stage only.",
    "setup": "Explicitly instrumented bridge manager/option setup, excluding native Render().",
    "verify_output": "Explicitly instrumented image decode/hash verification.",
    "restore_options": "Explicitly instrumented restoration of prior renderer options.",
    "bridge_total": "Bridge manager/options through restore; excludes preflight, queue, registration and transport.",
    "total": "Caller-reported total; consult timing_scope for its exact boundary.",
}
PHASES = {"warmup", "preview", "quality", "final"}
MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_PIXELS = 8192 * 8192


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stats(values: list[float]) -> dict:
    if not values:
        return {"status": "not_run", "sample_count": 0, "samples": []}
    return {"status": "measured", "sample_count": len(values), "samples": values,
            "median": statistics.median(values), "min": min(values), "max": max(values),
            "mean": statistics.mean(values),
            "sample_stdev": statistics.stdev(values) if len(values) > 1 else None}


def inspect_image(path: Path, settings: dict) -> dict:
    """Decode and hash an image; descriptive pixel statistics are not fidelity scores."""
    size = path.stat().st_size
    if size < 1 or size > MAX_IMAGE_BYTES:
        raise ValueError("Image is empty or exceeds the 256 MiB inspection limit")
    with Image.open(path) as image:
        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds the bounded pixel inspection limit")
        if image.format not in {"PNG", "JPEG"}:
            raise ValueError("Expected a native PNG or JPEG render output")
        if (width, height) != (settings["width"], settings["height"]):
            raise ValueError(f"Decoded {width}x{height} differs from requested dimensions")
        image.load()
        image_format, mode = image.format, image.mode
        sample = image.convert("RGB")
        sample.thumbnail((256, 256))
        luminance = ImageStat.Stat(sample.convert("L"))
        rgb_extrema = sample.getextrema()
    return {"path": str(path.resolve()), "format": image_format, "width": width, "height": height,
            "mode": mode, "bytes": size, "sha256": sha256(path),
            "luminance_mean_0_255": luminance.mean[0], "luminance_stddev_0_255": luminance.stddev[0],
            "constant_rgb_sample": all(low == high for low, high in rgb_extrema),
            "pixel_statistics_scope": "Downsampled descriptive diagnostics only; not noise, sharpness or scene-fidelity acceptance."}


def _settings(record: dict) -> dict:
    settings = dict(record.get("settings", record.get("arguments", {})))
    settings = {key: settings.get(key) for key in ("width", "height", "passes", "denoiser")}
    for key, maximum in (("width", 8192), ("height", 8192), ("passes", 10000)):
        if type(settings[key]) is not int or not 1 <= settings[key] <= maximum:
            raise ValueError(f"Invalid {key}")
    if type(settings["denoiser"]) is not bool:
        raise ValueError("denoiser must be a boolean")
    return settings


def _image_path(record: dict, base: Path) -> Path:
    path = record.get("image_path")
    if not path:
        result = record.get("result", record.get("output", {}))
        images = result.get("images", []) if isinstance(result, dict) else []
        path = images[0].get("path") if images else None
    if not isinstance(path, str) or not path:
        raise ValueError("No output image path was recorded")
    value = Path(path)
    return value if value.is_absolute() else base / value


def analyze(evidence: dict, *, base: Path = Path(".")) -> dict:
    records = evidence.get("records")
    if not isinstance(records, list):
        raise ValueError("Render evidence requires a records array")
    checked, seen, grouped = [], set(), {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("Every render record requires a nonempty id")
        if record["id"] in seen:
            raise ValueError("Duplicate render record id")
        seen.add(record["id"])
        phase = record.get("phase")
        review = record.get("visual_review", {"status": "not_run"})
        review = review if isinstance(review, dict) else {"status": "not_run"}
        review_status = review.get("status") if review.get("status") in {"pass", "fail", "not_run"} else "not_run"
        item = {"id": record["id"], "phase": phase, "reported_status": record.get("status", "not_run"),
                "status": "not_run", "artifact_verified": False, "finished_4k": False,
                "visual_review": {**review, "status": review_status}, "timings_ms": {},
                "timing_scope": record.get("timing_scope", {}),
                "gpu_execution": "unverified; no GPU telemetry is inferred from elapsed time"}
        try:
            if phase not in PHASES:
                raise ValueError("phase must be warmup, preview, quality or final")
            item["settings"] = _settings(record)
            timings = record.get("timings_ms", {})
            if not isinstance(timings, dict):
                raise ValueError("timings_ms must be an object")
            for key, value in timings.items():
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"Invalid elapsed time {key}")
                item["timings_ms"][key] = value
            native_result = record.get("result", record.get("output", {}))
            if isinstance(native_result, dict) and (native_result.get("ok") is False or native_result.get("is_error") is True or native_result.get("isError") is True):
                raise ValueError("Recorded native result reports an error")
            if record.get("status") not in {"pass", "completed"}:
                item["status"] = "fail" if record.get("status") in {"fail", "failed"} else "not_run"
                item["error"] = record.get("error", "Native render success was not recorded")
            else:
                item["image"] = inspect_image(_image_path(record, base), item["settings"])
                expected_hash = record.get("image_sha256")
                if expected_hash and expected_hash.lower() != item["image"]["sha256"]:
                    raise ValueError("Output image differs from the recorded SHA-256")
                item["artifact_verified"] = True
                item["status"] = "fail" if review_status == "fail" else "pass"
                item["finished_4k"] = (phase == "final" and item["image"]["width"] == 3840
                                       and item["image"]["height"] == 2160 and review_status == "pass")
        except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
            item.update(status="fail", error=str(exc))
        checked.append(item)
        if item["status"] == "pass" and phase != "warmup":
            identity = {"model": record.get("model", evidence.get("model")),
                        "scene_id": record.get("scene_id", evidence.get("scene_id")),
                        "phase": phase, "settings": item["settings"]}
            key = json.dumps(identity, sort_keys=True)
            group = grouped.setdefault(key, {**identity, "record_ids": [], "samples": {}})
            group["record_ids"].append(item["id"])
            for name, value in item["timings_ms"].items():
                group["samples"].setdefault(name, []).append(value)
    groups = []
    for group in grouped.values():
        groups.append({key: value for key, value in group.items() if key != "samples"}
                      | {"timings_ms": {key: stats(values) for key, values in group["samples"].items()}})
    return {"schema_version": 1, "run_id": evidence.get("run_id"), "model": evidence.get("model"),
            "scene_id": evidence.get("scene_id"), "environment": evidence.get("environment", {}),
            "timing_definitions": TIMING_SCOPES, "records": checked, "groups": groups,
            "summary": {"record_count": len(checked), "pass_count": sum(item["status"] == "pass" for item in checked),
                        "fail_count": sum(item["status"] == "fail" for item in checked),
                        "not_run_count": sum(item["status"] == "not_run" for item in checked),
                        "finished_4k_ids": [item["id"] for item in checked if item["finished_4k"]]},
            "limitations": ["Warm-ups are retained and excluded from grouped timings.",
                            "Different resolutions, passes and denoiser settings are different workloads; no equal-quality speedup is inferred.",
                            "Missing stage timings are unmeasured, not zero; native Render wall time is not GPU execution time.",
                            "Visual acceptance is a recorded human/agent review; decoding, size and pixel statistics cannot establish scene fidelity."]}


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown(report: dict) -> str:
    rows = ["# Recorded Visualize render evidence", "", f"Run: `{_cell(report.get('run_id'))}`.", "",
            "| ID | Phase | Verified output | Passes | Denoiser | Status | Visual review | Finished UHD 4K |",
            "|---|---|---|---:|---|---|---|---|"]
    for item in report["records"]:
        settings, image = item.get("settings", {}), item.get("image", {})
        dims = f"{image['width']} × {image['height']}" if item["artifact_verified"] else "unverified"
        rows.append(f"| {_cell(item['id'])} | {_cell(item['phase'])} | {dims} | {settings.get('passes', '—')} | {settings.get('denoiser', '—')} | {item['status']} | {item['visual_review']['status']} | {item['finished_4k']} |")
    rows += ["", "## Measured timing groups", "", "Each group has identical recorded model/scene identity, phase, dimensions, passes and denoiser settings. Missing model or scene identity limits comparability. Values are milliseconds.", "",
             "| Records | Timing boundary | Samples | Median | Min–max | Sample SD |", "|---|---|---:|---:|---:|---:|"]
    for group in report["groups"]:
        for key, timing in group["timings_ms"].items():
            sd = "—" if timing["sample_stdev"] is None else f"{timing['sample_stdev']:.3f}"
            rows.append(f"| {_cell(', '.join(group['record_ids']))} | {_cell(key)} | {timing['sample_count']} | {timing['median']:.3f} | {timing['min']:.3f}–{timing['max']:.3f} | {sd} |")
    rows += ["", "## Image evidence", ""]
    for item in report["records"]:
        if item.get("image"):
            image = item["image"]
            rows.append(f"- `{_cell(item['id'])}`: `{_cell(image['path'])}`; {image['bytes']} bytes; SHA-256 `{image['sha256']}`.")
        if item.get("error"):
            rows.append(f"- `{_cell(item['id'])}`: {_cell(item['error'])}.")
        notes = item["visual_review"].get("notes", [])
        if notes:
            rows.append(f"- `{_cell(item['id'])}` visual review: {_cell('; '.join(notes) if isinstance(notes, list) else notes)}.")
        if item["timing_scope"]:
            rows.append(f"- `{_cell(item['id'])}` recorded timing boundary: {_cell(item['timing_scope'])}.")
    rows += ["", "## Timing boundaries and limits", ""]
    rows.extend(f"- `{key}`: {value}" for key, value in TIMING_SCOPES.items())
    rows.extend(f"- {value}" for value in report["limitations"])
    return "\n".join(rows) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    targets = [path.resolve() for path in (args.output, args.markdown) if path is not None]
    if args.input.resolve() in targets or len(set(targets)) != len(targets):
        parser.error("Analysis outputs must be distinct from each other and the raw input")
    if any(path.exists() for path in targets):
        parser.error("Choose new analysis output paths; existing evidence is not overwritten")
    raw = args.input.read_bytes()
    report = analyze(json.loads(raw.decode("utf-8-sig")), base=args.input.resolve().parent)
    report["source_file"] = {"path": str(args.input), "sha256": hashlib.sha256(raw).hexdigest()}
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    if args.markdown:
        with args.markdown.open("x", encoding="utf-8") as stream:
            stream.write(markdown(report))
    print(json.dumps(report["summary"]))
    return 1 if report["summary"]["fail_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
