"""Derive returned-volume checks from immutable live benchmark JSON evidence.

This does not rerun CAD, revise raw call statuses/timings, or validate inertia.
Only an empty-arguments mass call is compared with its own record's post-edit
COM oracle. A missing/ambiguous/error response cannot receive a pass.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


MASS_TOOLS = frozenset({"get_mass_properties", "solidworks_get_mass_properties", "model_mass"})
RELATIVE_TOLERANCE = 1e-6
MM3_LABELS = frozenset({"mm\u00b3", "mm^3", "mm3"})


class EvidenceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _positive_number(value, label: str) -> float:
    if type(value) not in (int, float):
        raise EvidenceError("INVALID_NUMERIC_TYPE", f"{label} must be a JSON number, not {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise EvidenceError("NONFINITE_VALUE", f"{label} exceeds finite double range") from None
    if not math.isfinite(number):
        raise EvidenceError("NONFINITE_VALUE", f"{label} is not finite")
    if number <= 0:
        raise EvidenceError("NONPOSITIVE_VOLUME", f"{label} must be positive for this solid-volume relative comparison")
    return number


def _object(value, label: str) -> dict:
    if type(value) is not dict:
        raise EvidenceError("MISSING_OR_INVALID_OBJECT", f"{label} must be an object")
    return value


def _require_success(call: dict) -> dict:
    if call.get("status") != "pass" or call.get("is_error") is not False or call.get("measurement_status") != "measured":
        raise EvidenceError("CALL_NOT_SUCCESSFUL", "Raw call must be measured, status=pass and is_error=false")
    output = _object(call.get("output"), "output")
    for key, successful in (("ok", True), ("success", True), ("isError", False)):
        if key in output and output[key] is not successful:
            raise EvidenceError("OUTPUT_ERROR", "Mass output contains a failure or malformed success flag")
    if output.get("error") or output.get("errors"):
        raise EvidenceError("OUTPUT_ERROR", "Mass output contains an error")
    if "status" in output and type(output["status"]) is not str:
        raise EvidenceError("OUTPUT_ERROR", "Mass output status must be a string")
    if output.get("status") in {"error", "fail", "failed", "rejected", "running", "indeterminate"}:
        raise EvidenceError("OUTPUT_ERROR", "Mass output is not a completed success")
    return output


def extract_volume(adapter_id: str, call: dict) -> tuple[float, str]:
    """Use explicit adapter contracts; no regex/text scraping or unit guessing."""
    output = _require_success(call)
    tool = call.get("tool")
    if adapter_id in {"pro", "pro_before", "pro_v020", "hjbaard"}:
        if tool != "get_mass_properties" or output.get("ok") is not True:
            raise EvidenceError("CONTRACT_MISMATCH", "Native adapter requires successful get_mass_properties")
        properties = _object(output.get("mass_properties"), "mass_properties")
        value, unit_evidence = properties.get("volume_mm3"), "mass_properties.volume_mm3 field"
    elif adapter_id == "harrier":
        if tool != "solidworks_get_mass_properties":
            raise EvidenceError("CONTRACT_MISMATCH", "Harrier requires solidworks_get_mass_properties")
        value, unit_evidence = output.get("volume_mm3"), "volume_mm3 field"
    elif adapter_id == "soheil":
        if tool != "model_mass":
            raise EvidenceError("CONTRACT_MISMATCH", "soheil requires model_mass")
        value, unit_evidence = output.get("volumeMm3"), "volumeMm3 field"
    elif adapter_id == "andrew":
        if tool != "get_mass_properties" or output.get("status") != "success":
            raise EvidenceError("CONTRACT_MISMATCH", "Andrew requires successful get_mass_properties")
        properties = _object(output.get("mass_properties"), "mass_properties")
        volume = _object(properties.get("volume"), "mass_properties.volume")
        unit = volume.get("units")
        if type(unit) is not str or unit not in MM3_LABELS:
            raise EvidenceError("MISSING_OR_WRONG_UNIT", "Andrew volume must explicitly declare mm³, mm^3 or mm3")
        value, unit_evidence = volume.get("value"), "mass_properties.volume.units=" + unit
    else:
        raise EvidenceError("UNSUPPORTED_ADAPTER", f"No volume contract for adapter {adapter_id!r}")
    return _positive_number(value, "returned volume"), unit_evidence


def check_record(record: dict, source: dict, record_index: int, relative_tolerance: float = RELATIVE_TOLERANCE) -> dict:
    sample = {
        "adapter_id": record.get("id"), "repetition": record.get("repetition"),
        "run_id": source["run_id"], "source_file": source["path"], "source_sha256": source["sha256"],
        "record_index": record_index, "source_revision": record.get("source"),
        "status": "fail",
    }
    try:
        cases = _object(record.get("cases"), "cases")
        edited = _object(cases.get("parametric_edit"), "cases.parametric_edit")
        if edited.get("status") != "pass":
            raise EvidenceError("ORACLE_STAGE_NOT_SUCCESSFUL", "Post-edit stage must have passed")
        oracle = _object(edited.get("oracle"), "cases.parametric_edit.oracle")
        expected = _positive_number(oracle.get("volume_mm3"), "same-record post-edit oracle volume")
        sample["oracle_volume_mm3"] = expected
        calls = record.get("calls")
        if type(calls) is not list:
            raise EvidenceError("MASS_CALL_MISSING", "Record has no calls list")
        matches = [(index, call) for index, call in enumerate(calls) if type(call) is dict
                   and call.get("tool") in MASS_TOOLS and type(call.get("arguments")) is dict and call["arguments"] == {}]
        if not matches:
            raise EvidenceError("MASS_CALL_MISSING", "No mass-property call with explicit empty arguments")
        if len(matches) != 1:
            raise EvidenceError("MASS_CALL_AMBIGUOUS", "Multiple empty-arguments mass calls; refusing to select one silently")
        call_index, call = matches[0]
        sample.update(call_index=call_index, tool=call["tool"])
        returned, unit_evidence = extract_volume(record.get("id"), call)
        absolute_error = abs(returned - expected)
        relative_error = absolute_error / expected
        sample.update(returned_volume_mm3=returned, unit_evidence=unit_evidence,
                      absolute_error_mm3=absolute_error if math.isfinite(absolute_error) else None,
                      relative_error=relative_error if math.isfinite(relative_error) else None,
                      relative_tolerance=relative_tolerance)
        if not math.isfinite(relative_error) or relative_error > relative_tolerance:
            raise EvidenceError("VOLUME_MISMATCH", "Returned volume differs from same-record post-edit oracle beyond tolerance")
        sample.update(status="pass", reason="Returned volume agrees with same-record post-edit COM oracle")
    except EvidenceError as exc:
        sample.update(error_code=exc.code, reason=str(exc))
    except (TypeError, ValueError, OverflowError) as exc:
        sample.update(error_code="INVALID_EVIDENCE_SHAPE", reason=f"Malformed evidence: {type(exc).__name__}")
    return sample


def analyze_files(paths: list[Path], relative_tolerance: float = RELATIVE_TOLERANCE) -> dict:
    if type(relative_tolerance) not in (int, float) or not math.isfinite(relative_tolerance) or not 0 < relative_tolerance <= 1:
        raise ValueError("relative_tolerance must be finite, greater than zero and at most one")
    if not paths:
        raise ValueError("At least one raw input file is required")
    source_files, samples = [], []
    seen = set()
    for input_path in paths:
        path = Path(input_path)
        resolved = path.resolve(strict=True)
        if resolved in seen:
            raise ValueError("Duplicate raw input file")
        seen.add(resolved)
        raw = path.read_bytes()
        # Non-standard NaN/Infinity literals are loaded so affected samples fail
        # numeric validation instead of being silently accepted as evidence.
        document = json.loads(raw.decode("utf-8-sig"))
        if type(document) is not dict or type(document.get("run_id")) is not str or not document["run_id"]:
            raise ValueError(f"{path}: missing run_id")
        records = document.get("records")
        if type(records) is not list or not records:
            raise ValueError(f"{path}: records must be a nonempty list")
        source = {"path": path.as_posix(), "sha256": hashlib.sha256(raw).hexdigest(),
                  "run_id": document["run_id"], "record_count": len(records)}
        source_files.append(source)
        for index, record in enumerate(records):
            if type(record) is not dict:
                raise ValueError(f"{path}: record {index} must be an object")
            samples.append(check_record(record, source, index, relative_tolerance))
    by_adapter = defaultdict(Counter)
    for sample in samples:
        by_adapter[str(sample["adapter_id"])][sample["status"]] += 1
    counts = Counter(sample["status"] for sample in samples)
    return {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "check": "returned-volume-vs-same-record-post-edit-COM-oracle",
        "relative_tolerance": relative_tolerance,
        "scope": "Derived volume check only; original timings/statuses and raw inputs remain unchanged; no inertia validation.",
        "source_files": source_files,
        "summary": {"sample_count": len(samples), "pass_count": counts["pass"], "fail_count": counts["fail"],
                    "by_adapter": {name: {"sample_count": sum(values.values()), "pass_count": values["pass"],
                                           "fail_count": values["fail"]} for name, values in sorted(by_adapter.items())}},
        "samples": samples,
    }


def write_report(report: dict, output: Path, inputs: list[Path]) -> None:
    if output.resolve() in {Path(path).resolve() for path in inputs}:
        raise ValueError("Derived output must not overwrite a raw input file")
    data = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".measurements-", suffix=".json", dir=output.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path, default=[Path("benchmarks/results/live-before.json"),
                                                               Path("benchmarks/results/live-after.json")])
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/mass-volume-checks.json"))
    parser.add_argument("--relative-tolerance", type=float, default=RELATIVE_TOLERANCE)
    args = parser.parse_args()
    report = analyze_files(args.inputs, args.relative_tolerance)
    write_report(report, args.output, args.inputs)
    print(json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=True))
    return 0 if report["summary"]["fail_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
