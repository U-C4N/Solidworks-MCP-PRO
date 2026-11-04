"""Harness correctness with fake CAD only. Never connects to SolidWorks."""
from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
import math
from types import SimpleNamespace

import pytest

from benchmarks import run_live as live


def test_six_system_order_balances_positions_and_adjacent_pairs():
    specs = [{"id": str(index)} for index in range(6)]
    rows = live.balanced_orders(specs, 6, seed=123)
    assert len(rows) == 6
    for position in range(6):
        assert {row[position]["id"] for row in rows} == set(map(str, range(6)))
    pairs = Counter((a["id"], b["id"]) for row in rows for a, b in zip(row, row[1:]))
    assert len(pairs) == 30 and set(pairs.values()) == {1}
    assert rows == live.balanced_orders(specs, 6, seed=123)
    assert rows != live.balanced_orders(specs, 6, seed=456)
    assert specs == [{"id": str(index)} for index in range(6)]


def test_frozen_v020_has_distinct_import_root_version_and_required_file(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.3.0"\n')
    spec = next(item for item in live.default_specs(tmp_path) if item["id"] == "pro_v020")
    assert spec["expected_server_version"] == "0.2.0"
    assert "pro-v0.2.0" in spec["env"]["PYTHONPATH"]
    assert spec["required_files"][0].endswith("__init__.py")
    assert spec["source"]["wheel_sha256"] == "4dca5d879461e412440413a84f62a7ecec06cfe0af9dbc4db4b4d67a1cac2cf5"


def test_current_spec_uses_explicit_source_and_pyproject_version(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / ".local/pro-v0.2.0/src"))
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.8.7"\n')
    current = next(item for item in live.default_specs(tmp_path) if item["id"] == "pro")
    assert current["env"]["PYTHONPATH"] == str(tmp_path / "src")
    assert current["expected_server_version"] == "9.8.7"
    assert current["source"]["package_version"] == "9.8.7"
    assert current["required_files"] == [str(tmp_path / "src/solidworks_mcp_pro/__init__.py")]


async def test_measured_fixture_closes_before_separate_policy_fixture(tmp_path, monkeypatch):
    original = {"title": "UsersExistingPart", "path": "C:/user/existing.sldprt"}
    state = {"documents": [original], "active": original["title"], "volume": 0, "size": [0, 0, 0], "number": 0}
    events = []

    def snapshot(expected_title=None):
        if expected_title is not None:
            assert state["active"] == expected_title
        return {"documents": deepcopy(state["documents"]), "active_title": state["active"], "volume_mm3": state["volume"], "size_mm": state["size"]}

    @asynccontextmanager
    async def connect(spec):
        yield SimpleNamespace(benchmark_initialize={"response": {"serverInfo": {"version": "0.2.0"}}})

    class Driver:
        def __init__(self, session, kind, folder):
            self.calls, self.title = [], None

        def mark(self, action, error=False):
            events.append((action, self.title))
            value = {"tool": action, "arguments": {}, "elapsed_ms": 3, "is_error": error}
            self.calls.append(value)
            return value

        async def new(self):
            state["number"] += 1
            self.title = f"BenchmarkPart{state['number']}"
            state["documents"].append({"title": self.title, "path": ""})
            state["active"] = self.title
            self.mark("new")

        async def block(self):
            state.update(volume=32000, size=[80, 50, 8])
            self.mark("block")

        async def hole(self):
            state["volume"] = 32000 - math.pi * 25 * 8
            self.mark("hole")

        async def depth(self):
            state.update(volume=(4000 - math.pi * 25) * 12, size=[80, 50, 12])
            self.mark("edit")

        async def mass(self, extra=False):
            return self.mark("policy_mass" if extra else "mass", error=extra)

        async def export(self, path, allow_error=False):
            if path.exists():
                return self.mark("policy_export", error=True)
            path.write_text("ISO-10303-21;" + "x" * 200)
            return self.mark("export")

        async def close(self):
            self.mark("close")
            state["documents"] = [item for item in state["documents"] if item["title"] != self.title]
            state["active"] = original["title"]

    monkeypatch.setattr(live, "snapshot", snapshot)
    monkeypatch.setattr(live, "connect", connect)
    monkeypatch.setattr(live, "Driver", Driver)
    result = await live.measure({"id": "pro"}, 0, tmp_path / "scenario")
    assert live.cad_completed(result)
    assert len(result["calls"]) == 7
    assert len(result["policy_calls"]) == 5
    assert events.index(("close", "BenchmarkPart1")) < events.index(("new", "BenchmarkPart2"))
    assert all(result["cases"][name]["mcp_elapsed_ms"] == 3 for name in live.CAD_STAGES)
    assert result["policy_setup"]["excluded_from_cad_timing"]
    assert result["policy_cleanup"]["excluded_from_cad_timing"]
    assert result["remaining_documents"] == [original]
    assert result["cases"]["unknown_argument_rejected"]["status"] == "pass"
    assert result["cases"]["default_no_clobber"]["status"] == "pass"
    assert result["cases"]["close_own_document"]["call_index_start"] == 6


def fake_record(spec, repetition, phase):
    return {"id": spec["id"], "repetition": repetition, "phase": phase,
            "cases": {name: {"status": "pass"} for name in live.CAD_STAGES},
            "policy_setup": {"status": "pass"}, "policy_cleanup": {"status": "pass"}}


async def test_warmups_are_retained_and_excluded_from_measured_records(tmp_path):
    async def invoke(spec, repetition, folder, *, phase):
        return fake_record(spec, repetition, phase)

    specs = [{"id": str(index)} for index in range(6)]
    result = await live.execute_suite(specs, 6, tmp_path, measure_fn=invoke)
    assert result["status"] == "completed"
    assert len(result["warmup_records"]) == 6
    assert len(result["records"]) == 36
    assert len(result["execution_order"]) == 42
    assert all(record["phase"] == "measured" for record in result["records"])
    assert all(record["phase"] == "warmup" for record in result["warmup_records"])


async def test_warmup_failure_is_persisted_and_aborts_measured_run(tmp_path):
    async def invoke(spec, repetition, folder, *, phase):
        result = fake_record(spec, repetition, phase)
        result["cases"]["block"]["status"] = "fail"
        return result

    result = await live.execute_suite([{"id": "fixture"}], 6, tmp_path, measure_fn=invoke)
    assert result["status"] == "aborted"
    assert not result["records"] and len(result["warmup_records"]) == 1
    assert "Warm-up" in result["abort_reason"]


async def test_failed_warmup_policy_fixture_cleanup_also_aborts(tmp_path):
    async def invoke(spec, repetition, folder, *, phase):
        result = fake_record(spec, repetition, phase)
        result["policy_cleanup"]["status"] = "fail"
        return result

    result = await live.execute_suite([{"id": "fixture"}], 6, tmp_path, measure_fn=invoke)
    assert result["status"] == "aborted" and not result["records"]


async def test_existing_raw_output_is_never_overwritten(tmp_path):
    target = tmp_path / "existing.json"
    target.write_text("preserve")
    with pytest.raises(FileExistsError):
        await live.run(SimpleNamespace(output=target))
    assert target.read_text() == "preserve"
