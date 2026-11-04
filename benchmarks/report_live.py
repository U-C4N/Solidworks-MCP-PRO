"""Analyze recorded live CAD evidence; never attach to or modify SolidWorks."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import statistics

CAD_CASES = ("new_part", "block", "through_hole", "parametric_edit", "mass_query", "step_export")
CAD_STAGES = (*CAD_CASES, "close_own_document")
POLICY_CASES = ("unknown_argument_rejected", "default_no_clobber")
STATUSES = ("pass", "fail", "unsupported", "not_run")


def _status(case) -> str:
    value = case.get("status") if isinstance(case, dict) else None
    return value if value in STATUSES else "not_run"


def _valid_number(value) -> bool:
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def _stats(values: list[float]) -> dict:
    if not values:
        return {"status": "not_run", "sample_count": 0, "samples": []}
    return {"status": "measured", "sample_count": len(values), "samples": values,
            "median": round(statistics.median(values), 3), "min": round(min(values), 3), "max": round(max(values), 3),
            "mean": round(statistics.mean(values), 3),
            "sample_stdev": round(statistics.stdev(values), 3) if len(values) > 1 else None,
            "sample_variance": round(statistics.variance(values), 3) if len(values) > 1 else None}


def _counts(values) -> dict:
    found = Counter(values)
    return {status: found.get(status, 0) for status in STATUSES}


def analyze_live(report: dict) -> dict:
    """Summarize six CAD cases, two independent policies, and complete-run timing."""
    if not isinstance(report.get("records"), list):
        raise ValueError("Live evidence must contain a records array")
    repeats = report.get("repeats", 1)
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    grouped = {}
    participants = {item["id"]: item for item in report.get("participants", [])}
    seen = set()
    for record in report["records"]:
        key = (record.get("id"), record.get("repetition"))
        if not isinstance(key[0], str) or not key[0] or type(key[1]) is not int or key[1] < 0:
            raise ValueError("Each live record requires a server id and nonnegative integer repetition")
        if key in seen:
            raise ValueError(f"Duplicate live server/repetition: {key}")
        seen.add(key)
        grouped.setdefault(key[0], []).append(record)
    for identifier in participants:
        grouped.setdefault(identifier, [])
    servers = []
    for identifier, records in grouped.items():
        records.sort(key=lambda record: record["repetition"])
        expected = max(repeats, max((record["repetition"] for record in records), default=-1) + 1)
        by_repeat = {record["repetition"]: record for record in records}
        cases = {name: [] for name in (*CAD_STAGES, *POLICY_CASES)}
        timings = {name: [] for name in CAD_STAGES}
        total_times = []
        total_calls = []
        missing_repetitions = []
        observations = []
        for repetition in range(expected):
            record = by_repeat.get(repetition)
            if record is None:
                missing_repetitions.append(repetition)
            measured_cases = record.get("cases", {}) if record else {}
            for name in cases:
                item = measured_cases.get(name, {})
                cases[name].append(_status(item))
                if name in timings and _status(item) == "pass" and _valid_number(item.get("mcp_elapsed_ms")):
                    timings[name].append(item["mcp_elapsed_ms"])
            complete = all(_status(measured_cases.get(name)) == "pass" and _valid_number(measured_cases[name].get("mcp_elapsed_ms")) for name in CAD_STAGES)
            if complete:
                total_times.append(round(sum(measured_cases[name]["mcp_elapsed_ms"] for name in CAD_STAGES), 3))
                if all(type(measured_cases[name].get("tool_calls")) is int and measured_cases[name]["tool_calls"] >= 0 for name in CAD_STAGES):
                    total_calls.append(sum(measured_cases[name]["tool_calls"] for name in CAD_STAGES))
            if record and record.get("error"):
                observations.append({"repetition": repetition, "error": record["error"]})
            if record and record.get("cleanup"):
                observations.append({"repetition": repetition, "cleanup": record["cleanup"]})
        cad_counts = _counts(status for name in CAD_CASES for status in cases[name])
        cad_counts["total"] = expected * len(CAD_CASES)
        sources = []
        for record in records:
            source = record.get("source") or {}
            if source not in sources:
                sources.append(source)
        if not sources and identifier in participants:
            sources.append(participants[identifier].get("source") or {})
        servers.append({"id": identifier, "repetitions_expected": expected, "repetitions_recorded": len(records),
                        "missing_repetitions": missing_repetitions, "sources": sources,
                        "initialized_server_versions": sorted({str(record["initialization"]["response"]["serverInfo"]["version"])
                                                               for record in records if record.get("initialization", {}).get("response", {}).get("serverInfo", {}).get("version") is not None}),
                        "runtime_modes": sorted({str(record.get("runtime_mode") or participants.get(identifier, {}).get("runtime_mode") or "not recorded") for record in records}),
                        "cad_cases": cad_counts, "case_results": {name: _counts(cases[name]) for name in CAD_CASES},
                        "policies": {name: _counts(cases[name]) for name in POLICY_CASES},
                        "close_own_document": _counts(cases["close_own_document"]),
                        "stage_timing_ms": {name: _stats(values) for name, values in timings.items()},
                        "stdio_startup_ms": _stats([record["initialization"]["elapsed_ms"] for record in records
                                                   if _valid_number(record.get("initialization", {}).get("elapsed_ms"))]),
                        "complete_run_timing_ms": _stats(total_times), "complete_run_tool_calls": _stats(total_calls),
                        "observations": observations})
    return {"benchmark": report.get("benchmark", "live-cad"), "generated_at": report.get("generated_at"),
            "run_id": report.get("run_id"), "repeats": repeats, "servers": servers,
            "suite_status": report.get("status", "not recorded"), "abort_reason": report.get("abort_reason"),
            "order_design": report.get("order_design", "Historical fixed seeded shuffle; full position balance not asserted."),
            "fixture_design": report.get("fixture_design", "Historical fixture: close followed policy probes; see raw sequence."),
            "planned_order": report.get("planned_order", []), "execution_order": report.get("execution_order", []),
            "warmup_repetitions": report.get("warmup_repetitions", 0),
            "warmup_checks": [{"id": record["id"], "repetition": record["repetition"],
                               "cad_complete": all(_status(record.get("cases", {}).get(name)) == "pass" for name in CAD_STAGES),
                               "policy_setup": _status(record.get("policy_setup")),
                               "policy_cleanup": _status(record.get("policy_cleanup")),
                               "error": record.get("error", record.get("policy_error"))} for record in report.get("warmup_records", [])],
            "mass_query_validation": report.get("mass_query_validation", "Call success only; returned mass/volume units were not separately asserted. Modeling-stage volume and bounds were checked by the independent oracle."),
            "timing_definition": "Sum successful new_part, block, through_hole, parametric_edit, mass_query, step_export, close_own_document MCP timings; excludes policy probes and COM oracle time. Only fully passing seven-stage repetitions enter total speed statistics."}


def build_analysis(reports: list[tuple[str, dict]], mass_validation: dict | None = None) -> dict:
    labels = [label for label, _ in reports]
    if len(set(labels)) != len(labels):
        raise ValueError("Report labels must be unique")
    runs = [{"label": label, **analyze_live(report)} for label, report in reports]
    comparisons = []
    adapter_comparisons = []
    for run in runs:
        by_id = {server["id"]: server for server in run["servers"]}
        if "pro" not in by_id:
            continue
        current = by_id["pro"]
        for baseline_id in ("pro_v020", "pro_before"):
            if baseline_id not in by_id:
                continue
            baseline = by_id[baseline_id]
            for metric, old, new in [
                ("Mass query", baseline["stage_timing_ms"]["mass_query"], current["stage_timing_ms"]["mass_query"]),
                ("Complete CAD sequence", baseline["complete_run_timing_ms"], current["complete_run_timing_ms"]),
            ]:
                if old.get("median", 0) > 0 and new.get("median") is not None:
                    comparisons.append({"run": run["label"], "metric": metric, "baseline_id": baseline_id, "baseline_median_ms": old["median"],
                                        "current_median_ms": new["median"], "baseline_samples": old["sample_count"],
                                        "current_samples": new["sample_count"],
                                        "observed_change_percent": round((new["median"] / old["median"] - 1) * 100, 3)})
        new = current["complete_run_timing_ms"]
        for identifier, other in by_id.items():
            if identifier == "pro":
                continue
            old = other["complete_run_timing_ms"]
            if old.get("median", 0) > 0 and new.get("median") is not None:
                adapter_comparisons.append({"run": run["label"], "reference_id": identifier,
                                            "reference_median_ms": old["median"], "current_median_ms": new["median"],
                                            "reference_samples": old["sample_count"], "current_samples": new["sample_count"],
                                            "observed_change_percent": round((new["median"] / old["median"] - 1) * 100, 3)})
    result = {"schema_version": 1, "analysis": "live-cad-evidence", "runs": runs,
              "within_run_pro_comparisons": comparisons, "current_vs_adapters": adapter_comparisons}
    if mass_validation is not None:
        samples = mass_validation.get("samples")
        if not isinstance(samples, list) or any(sample.get("status") not in ("pass", "fail") for sample in samples):
            raise ValueError("Derived mass validation requires samples with explicit pass/fail statuses")
        result["returned_mass_verification"] = mass_validation
    return result


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("`", "'")


def _stat(stats: dict, field="median") -> str:
    value = stats.get(field)
    return "—" if value is None else f"{value:,.3f}"


def _count_text(counts: dict) -> str:
    return "/".join(str(counts.get(status, 0)) for status in STATUSES)


def _source_text(source: dict) -> str:
    parts = []
    repository = source.get("repository")
    if isinstance(repository, str) and repository.startswith(("https://", "http://")):
        safe_url = repository.replace("(", "%28").replace(")", "%29")
        parts.append(f"[repository]({safe_url})")
    parts.append(f"revision `{_cell(source.get('revision', 'not supplied'))}`")
    for key, label in [("package_version", "package"), ("release_tag", "release"), ("license", "license"), ("wheel_sha256", "frozen wheel SHA-256"), ("source_tree_sha256", "source tree SHA-256")]:
        if key in source:
            parts.append(f"{label} `{_cell(source[key])}`")
    return "; ".join(parts)


def render_live_markdown(analysis: dict) -> str:
    rows = ["# Measured SolidWorks MCP results", "",
            "This report summarizes recorded live benchmark runs. It separates CAD correctness, tool-call timing, cleanup, and two default policies; it does not combine them into a weighted score.", "",
            "Six CAD cases are checked per repetition: new document, 80 × 50 × 8 mm block, Ø10 through-hole, depth edit to 12 mm, mass query, and STEP export. Geometry checks use an independent COM mass/bounding-box oracle. Export validation is limited to the evidence recorded by the harness; it does not imply a successful round trip into another CAD kernel.", "",
            "## CAD acceptance", "",
            "| Run | Adapter | Repetitions recorded/expected | Pass / total | Fail | Unsupported | Not run | Close pass/fail/unsupported/not run |",
            "|---|---|---:|---:|---:|---:|---:|---:|"]
    for run in analysis.get("runs", []):
        for server in run["servers"]:
            cad = server["cad_cases"]
            rows.append(f"| {_cell(run['label'])} | {_cell(server['id'])} | {server['repetitions_recorded']}/{server['repetitions_expected']} | {cad['pass']}/{cad['total']} | {cad['fail']} | {cad['unsupported']} | {cad['not_run']} | {_count_text(server['close_own_document'])} |")
    rows += ["", "Absent stages and missing repetitions count as not run. Unsupported and not-run cases are never counted as passes. Adapter names identify the implemented call sequences; tool breadth and general modeling coverage are separate from this small common workload.", "", "## MCP timing", "",
             "The total includes new-document creation, block, hole, edit, mass query, STEP export, and close. It excludes policy probes, independent oracle reads, LLM inference, client startup, and retained warm-up scenarios. Only repetitions where all seven CAD stages passed enter complete-run timing; a fast failure cannot improve the median.", "",
             "| Run | Adapter | Complete timing samples | CAD total median ms | Min–max ms | Mean ± sample SD ms | CAD tool calls median (min–max) |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for run in analysis.get("runs", []):
        for server in run["servers"]:
            timing, calls = server["complete_run_timing_ms"], server["complete_run_tool_calls"]
            rows.append(f"| {_cell(run['label'])} | {_cell(server['id'])} | {timing['sample_count']} | {_stat(timing)} | {_stat(timing, 'min')}–{_stat(timing, 'max')} | {_stat(timing, 'mean')} ± {_stat(timing, 'sample_stdev')} | {_stat(calls)} ({_stat(calls, 'min')}–{_stat(calls, 'max')}) |")
    if any(server["stdio_startup_ms"]["sample_count"] for run in analysis.get("runs", []) for server in run["servers"]):
        rows += ["", "### Stdio process startup (excluded from CAD totals)", "",
                 "| Run | Adapter | Samples | Startup median ms | Min–max ms | Mean ± sample SD ms |",
                 "|---|---|---:|---:|---:|---:|"]
        for run in analysis["runs"]:
            for server in run["servers"]:
                startup = server["stdio_startup_ms"]
                rows.append(f"| {_cell(run['label'])} | {_cell(server['id'])} | {startup['sample_count']} | {_stat(startup)} | {_stat(startup, 'min')}–{_stat(startup, 'max')} | {_stat(startup, 'mean')} ± {_stat(startup, 'sample_stdev')} |")
        rows += ["", "Every repetition launches a fresh MCP subprocess. Startup measures process launch through MCP initialization. Servers can attach to CAD eagerly or on the first native call, so initialization and new-part costs fall in different stages. The CAD sum alone is not complete user-perceived startup latency; both measurements are shown. Application/cache warm-up scenarios remain excluded."]
    comparisons = analysis.get("within_run_pro_comparisons", [])
    if comparisons:
        rows += ["", "### Current PRO and frozen PRO in the same run", "",
                 "| Run | Baseline | Metric | Frozen median ms | Current median ms | Observed latency change | Samples frozen/current |",
                 "|---|---|---|---:|---:|---:|---:|"]
        for item in comparisons:
            rows.append(f"| {_cell(item['run'])} | {_cell(item.get('baseline_id', 'pro_before'))} | {_cell(item['metric'])} | {item['baseline_median_ms']:,.3f} | {item['current_median_ms']:,.3f} | {item['observed_change_percent']:+.1f}% | {item['baseline_samples']}/{item['current_samples']} |")
        rows += ["", "Negative latency change means a lower measured median; positive means higher. A faster mass query does not imply a faster full CAD sequence. Both observations are shown, without claiming statistical significance."]
    if analysis.get("current_vs_adapters"):
        rows += ["", "### Current PRO versus each same-run reference", "",
                 "| Run | Reference adapter | Reference CAD median ms | Current PRO CAD median ms | Observed latency change | Samples reference/current |",
                 "|---|---|---:|---:|---:|---:|"]
        for item in analysis["current_vs_adapters"]:
            rows.append(f"| {_cell(item['run'])} | {_cell(item['reference_id'])} | {item['reference_median_ms']:,.3f} | {item['current_median_ms']:,.3f} | {item['observed_change_percent']:+.1f}% | {item['reference_samples']}/{item['current_samples']} |")
        rows += ["", "These are observed medians for the same verified shape sequence. The adapters expose different operations and use different call counts; this table does not rank their overall feature coverage or reliability."]
    rows += ["", "### Stage medians (milliseconds)", "",
             "| Run | Adapter | New | Block | Hole | Edit | Mass | Export | Close |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for run in analysis.get("runs", []):
        for server in run["servers"]:
            rows.append("| " + " | ".join([_cell(run["label"]), _cell(server["id"]), *[_stat(server["stage_timing_ms"][name]) for name in CAD_STAGES]]) + " |")
    rows += ["", "Stage medians use only passing samples for that stage and can have different sample counts; complete sample arrays, means, sample standard deviations, sample variances (ms²), and minimum/maximum values are retained in the analysis JSON. Sample variance is unavailable for a single observation. Do not sum stage medians to reconstruct the median total.", "", "## Default policy probes", "",
             "Counts below are **pass / fail / unsupported / not run**. Unknown-argument rejection and no-clobber behavior are distinct policy choices, not extra geometry cases.", "",
             "| Run | Adapter | Unknown argument rejected | Existing export preserved and error reported |",
             "|---|---|---:|---:|"]
    for run in analysis.get("runs", []):
        for server in run["servers"]:
            rows.append(f"| {_cell(run['label'])} | {_cell(server['id'])} | {_count_text(server['policies']['unknown_argument_rejected'])} | {_count_text(server['policies']['default_no_clobber'])} |")
    rows += ["", "## Sources and observed failures", ""]
    verification = analysis.get("returned_mass_verification")
    if verification is not None:
        rows[-2:] = []
        rows += ["## Separate verification of returned mass-query volumes", "",
                 "These checks were computed after the runs from captured mass-query responses and the independent COM oracle in the same record. Original stage outcomes and measured timings were not changed. Only normal mass queries are included; deliberate unknown-argument policy probes are excluded.", "",
                 "| Adapter | Samples | Pass | Fail | Maximum relative volume error |",
                 "|---|---:|---:|---:|---:|"]
        grouped = {}
        for sample in verification["samples"]:
            grouped.setdefault(sample["adapter_id"], []).append(sample)
        for identifier, samples in grouped.items():
            errors = [sample["relative_error"] for sample in samples if _valid_number(sample.get("relative_error"))]
            maximum = f"{max(errors):.6g}" if errors else "—"
            passed = sum(sample["status"] == "pass" for sample in samples)
            rows.append(f"| {_cell(identifier)} | {len(samples)} | {passed} | {len(samples) - passed} | {maximum} |")
        passed_total = sum(sample["status"] == "pass" for sample in verification["samples"])
        rows += ["", f"Returned-volume checks passed: **{passed_total}/{len(verification['samples'])}**. " +
                 (f"Relative-error tolerance: `{verification['relative_tolerance']:g}`." if _valid_number(verification.get("relative_tolerance")) else "Consult the derived evidence for the acceptance tolerance.")]
        validation_path = str(analysis.get("mass_validation_path", "benchmarks/results/mass-volume-checks.json")).replace("\\", "/")
        rows += ["", f"The derived evidence records returned units, oracle values, source record/call indices, and the SHA-256 of each raw input. See [returned-volume checks](../{validation_path}).", ""]
        for source in verification.get("source_files", []):
            rows.append(f"- Raw source `{_cell(source.get('path', 'unspecified'))}`; SHA-256 `{_cell(source.get('sha256', 'unspecified'))}`.")
        rows += ["", "## Sources and observed failures", ""]
    for run in analysis.get("runs", []):
        rows.append(f"### {_cell(run['label'])}")
        rows.append("")
        rows.append(f"Run ID: `{_cell(run.get('run_id'))}`; recorded: {_cell(run.get('generated_at'))}; configured repetitions: {run['repeats']}.")
        rows.append("")
        rows.append("Order design: " + _cell(run["order_design"]))
        rows.append("")
        rows.append("Fixture design: " + _cell(run["fixture_design"]))
        rows.append("")
        if run.get("abort_reason"):
            rows += ["Run aborted: " + _cell(run["abort_reason"]), ""]
        if run["planned_order"]:
            rows += ["| Repetition | Planned adapter order |", "|---:|---|"]
            for index, order in enumerate(run["planned_order"]):
                rows.append(f"| {index} | " + " → ".join(_cell(identifier) for identifier in order) + " |")
            rows.append("")
        if run["warmup_checks"]:
            rows += ["Warm-ups are retained evidence and excluded from the measured tables above.", "",
                     "| Adapter | Warm-up repetition | CAD complete | Policy fixture setup | Policy fixture cleanup |",
                     "|---|---:|---|---|---|"]
            for check in run["warmup_checks"]:
                rows.append(f"| {_cell(check['id'])} | {check['repetition']} | {check['cad_complete']} | {_cell(check['policy_setup'])} | {_cell(check['policy_cleanup'])} |")
                if check.get("error"):
                    rows.append(f"\nWarm-up error for {_cell(check['id'])}: {_cell(check['error'])}\n")
            rows.append("")
        rows.append("Original mass-query stage validation: " + _cell(run["mass_query_validation"]))
        rows.append("")
        for server in run["servers"]:
            rows.append(f"- **{_cell(server['id'])}**: " + " / ".join(_source_text(source) for source in server["sources"]) + ".")
            if server["initialized_server_versions"]:
                rows.append(f"  Initialized server version(s): `{_cell(', '.join(server['initialized_server_versions']))}`; runtime: {_cell(', '.join(server['runtime_modes']))}.")
            for observation in server["observations"]:
                detail = observation.get("error", observation.get("cleanup", ""))
                rows.append(f"- {_cell(server['id'])}, repetition {observation['repetition']}: {_cell(detail)}.")
        rows.append("")
    rows += ["The initialized version above is the server's literal MCP `serverInfo.version` value; some upstream servers return the MCP SDK version. Repository revisions, release/package metadata, and frozen hashes identify the compared code independently.", "",
             "## Scope and limits", "",
             "The recorded repetitions provide descriptive medians, ranges, and sample variance, not a confidence interval or evidence of a general performance advantage. Planned counterbalancing controls position and within-row preceding-adapter exposure when a full design is completed; it does not eliminate process startup effects, cache history, application state, thermal changes, or background load. Separate historical before/after runs are sequential observations and are not randomized paired trials.", "",
             "This workload does not measure assemblies, drawings, sheet metal, arbitrary topology selection, undo/rollback, photorealistic rendering quality, or all supported CAD commands. Actual Visualize rendering and offline mesh preview require their own output evidence. More tools, stricter defaults, or faster results on these shapes do not establish universal superiority.", "",
             "No unsupported, missing, or failed scenario is silently reclassified as successful.", ""]
    if analysis.get("source_reports"):
        rows += ["Raw evidence: " + ", ".join(f"[{_cell(item['label'])}](../{str(item['path']).replace(chr(92), '/')})" for item in analysis["source_reports"]) + ".", ""]
    else:
        rows += ["See [protocol discovery results](../benchmarks/results/protocol-all.md), [before raw evidence](../benchmarks/results/live-before.json), and [after raw evidence](../benchmarks/results/live-after.json).", ""]
    if analysis.get("analysis_output"):
        rows += [f"[Derived analysis JSON](../{str(analysis['analysis_output']).replace(chr(92), '/')}).", ""]
    baseline_hashes = {source.get("wheel_sha256") for run in analysis.get("runs", []) for server in run["servers"]
                       if server["id"] == "pro_v020" for source in server["sources"]}
    baseline_hashes = {value for value in baseline_hashes if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)}
    if len(baseline_hashes) == 1:
        digest = next(iter(baseline_hashes))
        rows += ["## Reproduce the frozen 0.2.0 baseline", "",
                 "The source ZIP excludes `dist` and `.local`. Obtain the original PRO 0.2.0 wheel as a separate artifact; rebuilding current source is not a substitute for the measured baseline. Verify its hash before extracting it into the isolated import tree:", "",
                 "```powershell", "$BaselineWheel = 'C:\\path\\to\\solidworks_mcp_pro-0.2.0-py3-none-any.whl'",
                 f"$ExpectedHash = '{digest}'",
                 "if ((Get-FileHash -LiteralPath $BaselineWheel -Algorithm SHA256).Hash -ne $ExpectedHash) { throw 'Baseline wheel hash mismatch' }",
                 "$BaselineSource = Join-Path (Get-Location).Path '.local\\pro-v0.2.0\\src'",
                 "if (Test-Path -LiteralPath $BaselineSource) { throw 'Baseline extraction target already exists; inspect it before reuse' }",
                 "New-Item -ItemType Directory -Path $BaselineSource | Out-Null",
                 ".\\.venv\\Scripts\\python.exe -m zipfile -e $BaselineWheel $BaselineSource",
                 "if ($LASTEXITCODE -ne 0) { throw 'Baseline extraction failed' }", "```", "",
                 "The `pro_v020` spec sets `PYTHONPATH` only for its subprocess, requires the extracted package entry point, and checks initialized version `0.2.0`. Recreate the [competitor installations](competitor-review.md) and adjust manifest executable paths for your machine. Current and frozen PRO use the same dependency environment; their source trees remain separate.", "",
                 "The measured counterbalanced workload can then be rerun to a new output file:", "", "```powershell",
                 ".\\.venv\\Scripts\\python.exe -m benchmarks.run_live --live --servers pro,pro_v020,hjbaard,harrier,soheil,andrew --repeats 6 --warmup 1 --seed 20261103 --output benchmarks/results/performance-reproduction.json",
                 "```", "", "Raw result files are not overwritten. Keep the warm-up evidence, planned/actual order, source hashes, and initialization responses with every reproduction.", ""]
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--input", type=Path, help="Single same-run performance evidence file")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--mass-validation", type=Path, help="Separate derived returned-volume evidence; does not alter original timing data")
    args = parser.parse_args(argv)
    sources = [(label, path) for label, path in [("before", args.before), ("after", args.after), ("performance", args.input)] if path is not None]
    reports = [(label, json.loads(path.read_text(encoding="utf-8-sig"))) for label, path in sources]
    if not reports:
        parser.error("Provide --before, --after, or --input")
    mass_validation = json.loads(args.mass_validation.read_text(encoding="utf-8-sig")) if args.mass_validation else None
    analysis = build_analysis(reports, mass_validation)
    analysis["source_reports"] = [{"label": label, "path": str(path)} for label, path in sources]
    if args.mass_validation:
        analysis["mass_validation_path"] = str(args.mass_validation)
    if args.json_output:
        analysis["analysis_output"] = str(args.json_output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_live_markdown(analysis), encoding="utf-8")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(analysis, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
