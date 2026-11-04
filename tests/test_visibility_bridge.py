from types import SimpleNamespace

import pytest

from solidworks_mcp_pro import visibility_bridge as bridge


class Component:
    def __init__(self, name, value=1, fail=False):
        self.Name2, self._value, self.fail = name, value, fail

    def IsSuppressed(self):
        return False

    @property
    def Visible(self):
        return self._value

    @Visible.setter
    def Visible(self, value):
        if self.fail and value == 0:
            raise RuntimeError("Native assignment failed")
        self._value = value


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    parts = [Component("A-1"), Component("B-1")]
    model = SimpleNamespace(GetComponents=lambda top: parts, GraphicsRedraw2=lambda: None)
    monkeypatch.setattr(bridge, "_active", lambda *a, **k: (model, tmp_path / "copy.sldasm"))
    return parts


def test_batch_roundtrip_and_inventory(fixture):
    result = bridge.visibility(None, "copy.sldasm", [{"component": "A-1", "visible": False}])
    assert result["ok"] and result["verified"] and fixture[0].Visible == 0
    assert not result["saved"] and not result["rebuilt"]
    result = bridge.visibility(None, "copy.sldasm", [], dry_run=True)
    assert [item["visible"] for item in result["components"]] == [False, True]
    assert not result["mutation_attempted"]


def test_all_targets_preflight_before_mutation(fixture):
    with pytest.raises(bridge.AnimationError, match="COMPONENT_UNAVAILABLE"):
        bridge.visibility(None, "copy.sldasm", [{"component": "A-1", "visible": False}, {"component": "Missing", "visible": True}])
    assert fixture[0].Visible == 1


def test_partial_native_failure_restores_all(fixture):
    fixture[1].fail = True
    result = bridge.visibility(None, "copy.sldasm", [{"component": item.Name2, "visible": False} for item in fixture])
    assert not result["ok"] and result["rollback"]["verified"]
    assert all(item.Visible == 1 for item in fixture)


@pytest.mark.parametrize("items", [[], [{"component": "A-1", "visible": 1}], [{"component": "A-1", "visible": False}] * 2])
def test_invalid_assignments_rejected(fixture, items):
    with pytest.raises(bridge.AnimationError, match="INVALID_ARGUMENT"):
        bridge.visibility(None, "copy.sldasm", items)
    assert all(item.Visible == 1 for item in fixture)


def test_unknown_native_visibility_refused(fixture):
    fixture[0]._value = -1
    with pytest.raises(bridge.AnimationError, match="VISIBILITY_UNKNOWN"):
        bridge.visibility(None, "copy.sldasm", [{"component": "A-1", "visible": True}])
