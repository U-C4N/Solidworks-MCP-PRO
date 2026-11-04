from copy import deepcopy
import hashlib
import json

import pytest

from benchmarks.check_measurements import analyze_files, check_record, write_report


SOURCE = {"path": "raw.json", "sha256": "a" * 64, "run_id": "test-run"}


def record(adapter="hjbaard", value=47057.522203923065):
    expected = 47057.522203923065
    tool = {"harrier": "solidworks_get_mass_properties", "soheil": "model_mass"}.get(adapter, "get_mass_properties")
    if adapter == "andrew":
        output = {"status": "success", "mass_properties": {"volume": {"value": value, "units": "mm\u00b3"}}}
    elif adapter == "harrier":
        output = {"volume_mm3": value}
    elif adapter == "soheil":
        output = {"volumeMm3": value}
    else:
        output = {"ok": True, "mass_properties": {"volume_mm3": value}}
    call = {"tool": tool, "status": "pass", "is_error": False, "measurement_status": "measured",
            "output": output, "arguments": {}}
    return {"id": adapter, "repetition": 0, "source": {"revision": "pinned"},
            "cases": {"parametric_edit": {"status": "pass", "oracle": {"volume_mm3": expected}}},
            "calls": [call]}


@pytest.mark.parametrize("adapter", ["hjbaard", "pro", "pro_before", "pro_v020", "harrier", "soheil", "andrew"])
def test_all_explicit_contracts_match_own_record(adapter):
    sample = check_record(record(adapter), SOURCE, 4)
    assert sample["status"] == "pass" and sample["relative_error"] == 0
    assert sample["record_index"] == 4 and sample["source_sha256"] == SOURCE["sha256"]
    assert sample["run_id"] == "test-run" and sample["unit_evidence"]


def test_harrier_two_decimal_precision_passes_requested_tolerance():
    sample = check_record(record("harrier", 47057.52), SOURCE, 0)
    assert sample["status"] == "pass"
    assert 0 < sample["relative_error"] < 1e-6


@pytest.mark.parametrize("value", [None, "47057.522203923065", True, float("nan"), float("inf"), -1, 0])
def test_invalid_or_missing_volume_never_passes(value):
    sample = check_record(record(value=value), SOURCE, 0)
    assert sample["status"] == "fail" and sample["error_code"]
    json.dumps(sample, allow_nan=False)


@pytest.mark.parametrize("unit", [None, "m3", "mm2", "kg", "", 3])
def test_andrew_wrong_or_missing_units_never_pass(unit):
    item = record("andrew")
    volume = item["calls"][0]["output"]["mass_properties"]["volume"]
    if unit is None:
        del volume["units"]
    else:
        volume["units"] = unit
    sample = check_record(item, SOURCE, 0)
    assert sample["status"] == "fail" and sample["error_code"] == "MISSING_OR_WRONG_UNIT"


@pytest.mark.parametrize("mutation", [
    {"is_error": True}, {"status": "fail"}, {"measurement_status": "not_measured"}, {"is_error": None},
])
def test_unsuccessful_raw_call_never_passes(mutation):
    item = record()
    item["calls"][0].update(mutation)
    assert check_record(item, SOURCE, 0)["status"] == "fail"


@pytest.mark.parametrize("failure", [{"ok": False}, {"isError": True}, {"error": "failed"}, {"status": "running"}])
def test_explicit_output_error_never_passes(failure):
    item = record()
    item["calls"][0]["output"].update(failure)
    assert check_record(item, SOURCE, 0)["status"] == "fail"


def test_only_empty_arguments_mass_call_is_selected_and_oracle_is_post_edit():
    item = record()
    unrelated = deepcopy(item["calls"][0])
    unrelated["arguments"] = {"unexpected_benchmark_argument": 123}
    unrelated["output"]["mass_properties"]["volume_mm3"] = 1
    item["calls"].insert(0, unrelated)
    item["cases"]["block"] = {"oracle": {"volume_mm3": 32000}}
    sample = check_record(item, SOURCE, 0)
    assert sample["status"] == "pass" and sample["call_index"] == 1
    assert sample["oracle_volume_mm3"] != 32000


def test_missing_and_duplicate_mass_calls_never_pass():
    item = record()
    item["calls"][0]["arguments"] = None
    assert check_record(item, SOURCE, 0)["error_code"] == "MASS_CALL_MISSING"
    item = record()
    item["calls"].append(deepcopy(item["calls"][0]))
    assert check_record(item, SOURCE, 0)["error_code"] == "MASS_CALL_AMBIGUOUS"


def test_mismatch_and_invalid_oracle_never_pass():
    assert check_record(record(value=32000), SOURCE, 0)["error_code"] == "VOLUME_MISMATCH"
    for value in (None, True, "47057.52", float("nan"), 0):
        item = record()
        item["cases"]["parametric_edit"]["oracle"]["volume_mm3"] = value
        assert check_record(item, SOURCE, 0)["status"] == "fail"


def test_files_preserve_raw_hashes_run_ids_and_every_sample(tmp_path):
    inputs = [tmp_path / "before.json", tmp_path / "after.json"]
    for index, path in enumerate(inputs):
        path.write_text(json.dumps({"run_id": f"run-{index}", "records": [record(), record("andrew")]}), encoding="utf-8")
    originals = [path.read_bytes() for path in inputs]
    report = analyze_files(inputs)
    assert report["summary"] == {"sample_count": 4, "pass_count": 4, "fail_count": 0,
                                  "by_adapter": {"andrew": {"sample_count": 2, "pass_count": 2, "fail_count": 0},
                                                 "hjbaard": {"sample_count": 2, "pass_count": 2, "fail_count": 0}}}
    assert {sample["run_id"] for sample in report["samples"]} == {"run-0", "run-1"}
    assert [entry["sha256"] for entry in report["source_files"]] == [hashlib.sha256(raw).hexdigest() for raw in originals]
    target = tmp_path / "derived.json"
    write_report(report, target, inputs)
    assert json.loads(target.read_text())["summary"]["sample_count"] == 4
    assert [path.read_bytes() for path in inputs] == originals
    with pytest.raises(ValueError, match="raw input"):
        write_report(report, inputs[0], inputs)


def test_unknown_adapter_and_missing_source_metadata_do_not_succeed(tmp_path):
    assert check_record(record("unknown"), SOURCE, 0)["error_code"] == "UNSUPPORTED_ADAPTER"
    path = tmp_path / "raw.json"
    path.write_text(json.dumps({"records": [record()]}))
    with pytest.raises(ValueError, match="run_id"):
        analyze_files([path])


@pytest.mark.parametrize("tolerance", [0, -1, float("nan"), float("inf"), True])
def test_invalid_tolerance_rejected(tmp_path, tolerance):
    with pytest.raises(ValueError, match="relative_tolerance"):
        analyze_files([tmp_path / "unused"], tolerance)


@pytest.mark.parametrize("failure", [{"ok": 1}, {"isError": "false"}, {"status": []}])
def test_malformed_success_flags_do_not_pass_or_crash(failure):
    item = record("harrier")
    item["calls"][0]["output"].update(failure)
    sample = check_record(item, SOURCE, 0)
    assert sample["status"] == "fail"
    json.dumps(sample, allow_nan=False)


def test_overflowed_relative_error_is_failed_but_report_stays_json_safe():
    item = record(value=1e308)
    item["cases"]["parametric_edit"]["oracle"]["volume_mm3"] = 1e-308
    sample = check_record(item, SOURCE, 0)
    assert sample["status"] == "fail" and sample["error_code"] == "VOLUME_MISMATCH"
    assert sample["relative_error"] is None
    json.dumps(sample, allow_nan=False)
