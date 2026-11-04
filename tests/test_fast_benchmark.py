from copy import deepcopy
import json

import pytest

from benchmarks import run_fast_workflows as benchmark
from solidworks_mcp_pro.fast_tools import COMPACT_TOOLS


class DiscoveryBackend:
    def __init__(self):
        self.list_calls = 0
        self.native_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        return benchmark._fixture_tools()

    async def call(self, name, arguments):
        self.native_calls += 1
        pytest.fail("Benchmark must never invoke discovery backend CAD tools")


async def test_catalog_profiles_use_actual_supplied_descriptors_without_cad_calls(tmp_path):
    backend = DiscoveryBackend()
    report, native = await benchmark.measure_catalogs(tmp_path, native_backend=backend)
    assert backend.list_calls == 1 and backend.native_calls == 0
    assert report["native_cad_tools_invoked"] == 0
    full, compact = report["profiles"]["full"], report["profiles"]["compact"]
    assert full["tool_count"] == len(native) + 35  # Full profile includes all 35 PRO workflows.
    assert compact["tool_count"] == len(COMPACT_TOOLS)
    assert set(compact["tool_names"]) == COMPACT_TOOLS
    assert compact["catalog_bytes"] < full["catalog_bytes"]
    assert full["input_schema_bytes"] < full["catalog_bytes"]


@pytest.mark.parametrize("scenario,legacy_calls,new_calls,render_calls", [
    ("diagnosis", 4, 1, 0), ("color_32", 32, 1, 0),
    ("normal_preview_final", 12, 3, 2), ("exploded_preview_final", 15, 3, 2),
])
async def test_scenarios_match_settings_calls_and_restore_state(tmp_path, scenario, legacy_calls, new_calls, render_calls):
    legacy = await benchmark.measure_scenario(tmp_path / "legacy", scenario, high_level=False)
    modern = await benchmark.measure_scenario(tmp_path / "modern", scenario, high_level=True)
    assert legacy["scenario_spec"] == modern["scenario_spec"] == benchmark.scenario_spec(scenario)
    assert legacy["render_settings"] == modern["render_settings"]
    assert legacy["synthetic_render_calls"] == modern["synthetic_render_calls"] == render_calls
    if render_calls:
        assert modern["render_settings"] == [benchmark.PREVIEW, benchmark.FINAL]
        assert modern["primary"]["poll_image_blocks"] == 0
        assert modern["primary"]["image_blocks"] == 1
        assert legacy["primary"]["poll_image_blocks"] == 2
    assert legacy["primary"]["public_calls"] == legacy_calls
    assert modern["primary"]["public_calls"] == new_calls
    assert legacy["final_colors"] == modern["final_colors"]
    assert legacy["temporary_state_restored"] and modern["temporary_state_restored"]
    assert modern["optional_evidence_retrieval"]["public_calls"] == 1
    assert modern["optional_evidence_retrieval"]["backend_calls_added"] == 0
    assert modern["native_cad_wall_ms"] is modern["native_render_wall_ms"] is None


async def test_summary_and_detail_metrics_are_deterministic_across_repeats(tmp_path):
    backend = DiscoveryBackend()
    report = await benchmark.run_benchmark(tmp_path, repeats=2, native_backend=backend)
    assert all(item["normalized_metrics_stable"] for item in report["scenarios"])
    assert all(sample["high_level"]["optional_evidence_retrieval"]["backend_calls_added"] == 0
               for item in report["scenarios"] for sample in item["samples"])
    assert report["tokenizer"]["status"] == "not_measured"
    assert backend.native_calls == 0
    json.dumps(report, allow_nan=False)


def test_scenario_spec_returns_independent_settings_and_rejects_unknown():
    one = benchmark.scenario_spec("exploded_preview_final")
    original = deepcopy(one)
    one["preview"]["passes"] = 1
    one["transforms"][0]["position_mm"][0] = 100000
    assert benchmark.scenario_spec("exploded_preview_final") == original
    with pytest.raises(ValueError):
        benchmark.scenario_spec("not-a-scenario")


@pytest.mark.parametrize("repeats", [0, 26, True, 1.5])
async def test_invalid_repeats_rejected_before_discovery(tmp_path, repeats):
    backend = DiscoveryBackend()
    with pytest.raises(ValueError):
        await benchmark.run_benchmark(tmp_path, repeats=repeats, native_backend=backend)
    assert backend.list_calls == 0


def test_optional_tokenizer_missing_is_reported_without_download(monkeypatch):
    monkeypatch.setattr(benchmark.importlib.util, "find_spec", lambda name: None)
    counter, status = benchmark.optional_tokenizer()
    assert counter is None and status["status"] == "not_measured"


async def test_token_counts_are_explicit_separate_metrics(tmp_path):
    report, _ = await benchmark.measure_catalogs(tmp_path, native_backend=DiscoveryBackend(), count_tokens=len)
    assert report["profiles"]["compact"]["catalog_text_tokens"] > 0
    sample = await benchmark.measure_scenario(tmp_path / "scenario", "diagnosis", high_level=True, count_tokens=len)
    assert sample["primary"]["request_text_tokens"] > 0
    assert sample["primary"]["response_text_envelope_tokens"] > 0
