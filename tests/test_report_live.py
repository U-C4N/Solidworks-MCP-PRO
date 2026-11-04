import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("report_live", ROOT / "benchmarks" / "report_live.py")
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def record(repetition=0, identifier="example", elapsed=10):
    return {"id": identifier, "repetition": repetition, "source": {"revision": "fixture"}, "status": "completed", "cases": {
        **{name: {"status": "pass", "mcp_elapsed_ms": elapsed, "tool_calls": 2} for name in reporter.CAD_STAGES},
        "unknown_argument_rejected": {"status": "pass", "mcp_elapsed_ms": 9999},
        "default_no_clobber": {"status": "fail", "mcp_elapsed_ms": 9999},
    }}


def test_six_case_counts_and_seven_stage_timing_exclude_policies():
    report = {"repeats": 3, "records": [record(0, elapsed=10), record(1, elapsed=30), record(2, elapsed=20)]}
    server = reporter.analyze_live(report)["servers"][0]
    assert server["cad_cases"] == {"pass": 18, "fail": 0, "unsupported": 0, "not_run": 0, "total": 18}
    assert server["complete_run_timing_ms"] == {"status": "measured", "sample_count": 3, "samples": [70, 210, 140], "median": 140, "min": 70, "max": 210,
                                               "mean": 140, "sample_stdev": 70, "sample_variance": 4900}
    assert server["complete_run_tool_calls"]["median"] == 14
    assert server["policies"]["unknown_argument_rejected"]["pass"] == 3
    assert server["policies"]["default_no_clobber"]["fail"] == 3


def test_failure_cannot_look_faster_than_a_complete_run():
    good = record(0, elapsed=20)
    bad = record(1, elapsed=1)
    bad["cases"]["block"]["status"] = "fail"
    bad["cases"].pop("step_export")
    server = reporter.analyze_live({"repeats": 2, "records": [good, bad]})["servers"][0]
    assert server["complete_run_timing_ms"]["samples"] == [140]
    assert server["cad_cases"]["fail"] == 1
    assert server["cad_cases"]["not_run"] == 1
    assert server["stage_timing_ms"]["block"]["samples"] == [20]


def test_missing_repetition_and_unsupported_are_never_passes():
    item = record(0)
    item["cases"]["parametric_edit"]["status"] = "unsupported"
    server = reporter.analyze_live({"repeats": 3, "records": [item]})["servers"][0]
    assert server["cad_cases"] == {"pass": 5, "fail": 0, "unsupported": 1, "not_run": 12, "total": 18}
    assert server["missing_repetitions"] == [1, 2]
    assert server["complete_run_timing_ms"]["status"] == "not_run"
    assert server["policies"]["unknown_argument_rejected"]["not_run"] == 2


def test_missing_cleanup_does_not_enter_complete_speed_samples():
    item = record()
    item["cases"].pop("close_own_document")
    server = reporter.analyze_live({"repeats": 1, "records": [item]})["servers"][0]
    assert server["cad_cases"]["pass"] == 6
    assert server["complete_run_timing_ms"]["sample_count"] == 0
    assert server["close_own_document"]["not_run"] == 1


@pytest.mark.parametrize("elapsed", [float("nan"), float("inf"), -1, True, None])
def test_invalid_times_are_not_measured(elapsed):
    item = record()
    item["cases"]["block"]["mcp_elapsed_ms"] = elapsed
    server = reporter.analyze_live({"repeats": 1, "records": [item]})["servers"][0]
    assert server["stage_timing_ms"]["block"]["status"] == "not_run"
    assert server["complete_run_timing_ms"]["sample_count"] == 0


def test_duplicate_records_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        reporter.analyze_live({"repeats": 1, "records": [record(), record()]})


def test_markdown_preserves_failure_evidence_and_limitations():
    item = record(identifier="adapter | name")
    item["error"] = "observed failure"
    analysis = reporter.build_analysis([("before", {"repeats": 1, "records": [item]})])
    markdown = reporter.render_live_markdown(analysis)
    assert "adapter \\| name" in markdown
    assert "observed failure" in markdown
    assert "not a confidence interval" in markdown
    assert "not establish universal superiority" in markdown
    assert "does not combine them into a weighted score" in markdown


def test_comparison_shows_faster_mass_and_slower_total_without_hiding_either():
    baseline = record(identifier="pro_before", elapsed=10)
    current = record(identifier="pro", elapsed=20)
    current["cases"]["mass_query"]["mcp_elapsed_ms"] = 5
    analysis = reporter.build_analysis([("after", {"repeats": 1, "records": [baseline, current]})])
    comparison = {row["metric"]: row for row in analysis["within_run_pro_comparisons"]}
    assert comparison["Mass query"]["observed_change_percent"] == -50
    assert comparison["Complete CAD sequence"]["observed_change_percent"] > 0
    markdown = reporter.render_live_markdown(analysis)
    assert "-50.0%" in markdown
    assert "does not imply a faster full CAD sequence" in markdown
    assert "Call success only" in markdown


def test_derived_mass_evidence_is_separate_and_preserves_original_timing():
    item = record()
    evidence = {"schema_version": 1, "source_files": [{"path": "live-before.json", "sha256": "abc"}], "samples": [
        {"adapter_id": "example", "status": "pass", "relative_error": 1e-8},
        {"adapter_id": "example", "status": "fail", "relative_error": 0.1},
    ]}
    analysis = reporter.build_analysis([("before", {"repeats": 1, "records": [item]})], evidence)
    assert analysis["runs"][0]["servers"][0]["complete_run_timing_ms"]["median"] == 70
    assert analysis["returned_mass_verification"] == evidence
    markdown = reporter.render_live_markdown(analysis)
    assert "| example | 2 | 1 | 1 | 0.1 |" in markdown
    assert "Original stage outcomes and measured timings were not changed" in markdown
    assert "SHA-256 `abc`" in markdown


def test_invalid_derived_mass_status_is_rejected():
    with pytest.raises(ValueError, match="pass/fail"):
        reporter.build_analysis([], {"samples": [{"status": "unknown"}]})


def test_current_compares_with_v020_and_all_four_rivals_in_same_run():
    identifiers = ["pro", "pro_v020", "hjbaard", "harrier", "soheil", "andrew"]
    records = [record(rep, identifier, elapsed=10 if identifier == "pro" else 20) for rep in range(6) for identifier in identifiers]
    analysis = reporter.build_analysis([("performance", {"repeats": 6, "records": records})])
    assert {item["reference_id"] for item in analysis["current_vs_adapters"]} == set(identifiers) - {"pro"}
    assert all(item["observed_change_percent"] == -50 for item in analysis["current_vs_adapters"])
    assert all(item["baseline_id"] == "pro_v020" for item in analysis["within_run_pro_comparisons"])
    markdown = reporter.render_live_markdown(analysis)
    assert "pro_v020" in markdown and "Mean ± sample SD ms" in markdown
    assert "Three repetitions" not in markdown


def test_warmups_are_visible_but_not_in_performance_samples():
    warmup = record(0, elapsed=1000)
    warmup.update(policy_setup={"status": "pass"}, policy_cleanup={"status": "pass"})
    analysis = reporter.build_analysis([("performance", {"repeats": 1, "records": [record(0, elapsed=10)], "warmup_records": [warmup]})])
    server = analysis["runs"][0]["servers"][0]
    assert server["complete_run_timing_ms"]["samples"] == [70]
    assert server["complete_run_timing_ms"]["sample_variance"] is None
    markdown = reporter.render_live_markdown(analysis)
    assert "Warm-ups are retained evidence" in markdown
    assert "| example | 0 | True | pass | pass |" in markdown


def test_aborted_warmup_keeps_all_planned_participants_not_run():
    report = {"repeats": 6, "records": [], "participants": [{"id": "pro", "source": {"revision": "fixture"}}, {"id": "pro_v020"}],
              "status": "aborted", "abort_reason": "Warm-up failed", "planned_order": [["pro", "pro_v020"]]}
    analysis = reporter.build_analysis([("performance", report)])
    assert len(analysis["runs"][0]["servers"]) == 2
    for server in analysis["runs"][0]["servers"]:
        assert server["cad_cases"]["not_run"] == 36
        assert server["complete_run_timing_ms"]["sample_count"] == 0
    assert "Run aborted: Warm-up failed" in reporter.render_live_markdown(analysis)


def test_stdio_startup_reported_separately_from_cad_total():
    item = record(elapsed=10)
    item["initialization"] = {"elapsed_ms": 9876, "response": {"serverInfo": {"version": "0.3.0"}}}
    item["runtime_mode"] = "native"
    analysis = reporter.build_analysis([("performance", {"repeats": 1, "records": [item]})])
    server = analysis["runs"][0]["servers"][0]
    assert server["stdio_startup_ms"]["median"] == 9876
    assert server["complete_run_timing_ms"]["median"] == 70
    markdown = reporter.render_live_markdown(analysis)
    assert "Stdio process startup (excluded from CAD totals)" in markdown
    assert "9,876.000" in markdown
    assert server["initialized_server_versions"] == ["0.3.0"]
    assert server["runtime_modes"] == ["native"]


def test_frozen_v020_reproduction_preserves_wheel_identity():
    item = record(identifier="pro_v020")
    digest = "4dca5d879461e412440413a84f62a7ecec06cfe0af9dbc4db4b4d67a1cac2cf5"
    item["source"]["wheel_sha256"] = digest
    analysis = reporter.build_analysis([("performance", {"repeats": 1, "records": [item]})])
    markdown = reporter.render_live_markdown(analysis)
    assert "source ZIP excludes `dist` and `.local`" in markdown
    assert f"$ExpectedHash = '{digest}'" in markdown
    assert "-m zipfile -e $BaselineWheel $BaselineSource" in markdown
    assert "performance-reproduction.json" in markdown
