"""Process-barrier tests use deterministic clocks and process snapshots only."""

import pytest

from solidworks_mcp_pro import renderer_process as bridge


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 0.1
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(bridge.time, "monotonic", value.monotonic)
    monkeypatch.setattr(bridge.time, "sleep", value.sleep)
    return value


def test_idle_requires_half_second_stable_absence(monkeypatch, clock):
    monkeypatch.setattr(bridge, "renderer_processes", lambda: [])
    result = bridge.wait_for_renderer_idle()
    assert result["ok"]
    assert result["stable_absence_ms"] >= 500
    assert result["elapsed_ms"] == 500
    assert result["poll_count"] >= 6
    assert result["observed_pids"] == []
    assert not result["processes_terminated"] and not result["native_render_started"]


def test_previous_renderer_exits_before_stability_window(monkeypatch, clock):
    monkeypatch.setattr(bridge, "renderer_processes", lambda: [{"pid": 42, "name": "OfflineRenderer.exe"}]
                        if clock.now < 0.3 else [])
    result = bridge.wait_for_renderer_idle(2)
    assert result["ok"]
    assert result["elapsed_ms"] >= 800
    assert result["observed_pids"] == [42]
    assert result["remaining_processes"] == []


def test_renderer_reappearing_resets_absence_window(monkeypatch, clock):
    def processes():
        if 0.2 <= clock.now < 0.4:
            return [{"pid": 50, "name": "OfflineRenderer.exe"}]
        return []

    monkeypatch.setattr(bridge, "renderer_processes", processes)
    result = bridge.wait_for_renderer_idle(2)
    assert result["ok"]
    assert result["elapsed_ms"] >= 900
    assert result["observed_pids"] == [50]


def test_busy_timeout_preserves_process_and_blocks_native_entry(monkeypatch, clock):
    processes = [{"pid": 99, "name": "OfflineRenderer.exe"}]
    monkeypatch.setattr(bridge, "renderer_processes", lambda: processes)
    result = bridge.wait_for_renderer_idle(0.8)
    assert not result["ok"]
    assert result["code"] == "VISUALIZE_RENDERER_BUSY"
    assert result["elapsed_ms"] == 800
    assert result["remaining_processes"] == processes
    assert result["observed_pids"] == [99]
    assert not result["processes_terminated"] and not result["native_render_started"]


def test_exit_too_close_to_deadline_is_not_stable_idle(monkeypatch, clock):
    monkeypatch.setattr(bridge, "renderer_processes", lambda: [{"pid": 1, "name": "OfflineRenderer.exe"}]
                        if clock.now < 0.7 else [])
    result = bridge.wait_for_renderer_idle(1)
    assert not result["ok"]
    assert result["remaining_processes"] == []
    assert result["stable_absence_ms"] < 500
    assert result["elapsed_ms"] == 1000


def test_enumeration_failure_fails_closed(monkeypatch, clock):
    def unavailable():
        raise OSError("snapshot denied")

    monkeypatch.setattr(bridge, "renderer_processes", unavailable)
    result = bridge.wait_for_renderer_idle()
    assert not result["ok"] and result["code"] == "RENDERER_PROCESS_INSPECTION_FAILED"
    assert "snapshot denied" in result["error"]
    assert not clock.sleeps


@pytest.mark.parametrize("timeout", [0, 0.49, 61, -1, True, None, "60", float("nan"), float("inf")])
def test_timeout_bounds_reject_before_process_inspection(monkeypatch, timeout):
    monkeypatch.setattr(bridge, "renderer_processes", lambda: pytest.fail("invalid timeout must not inspect processes"))
    with pytest.raises(ValueError, match="0.5..60"):
        bridge.wait_for_renderer_idle(timeout)


def test_all_observed_pids_reported_without_interference(monkeypatch, clock):
    monkeypatch.setattr(bridge, "renderer_processes", lambda: [
        {"pid": 21, "name": "OfflineRenderer.exe"}, {"pid": 12, "name": "OfflineRenderer.exe"}
    ] if clock.now < 0.1 else [])
    result = bridge.wait_for_renderer_idle(2)
    assert result["ok"] and result["observed_pids"] == [12, 21]
