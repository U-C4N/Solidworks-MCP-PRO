from contextlib import contextmanager

import pytest
from solidworks_mcp.errors import SolidWorksError

from solidworks_mcp_pro.native_guard import run_guarded


class Session:
    def __init__(self, busy=False):
        self.busy = busy
        self._sw = object()
        self.quiet = False
        self.events = []
        self.snapshot_error = None
        self.left = []

    def _reattach_if_gone(self):
        self.events.append("reattach")

    def _command_in_progress(self, flag):
        if flag is None or flag == self.busy:
            return None
        previous, self.busy = self.busy, flag
        self.events.append(("busy", flag))
        return previous

    @contextmanager
    def _quiet_ui(self):
        self.quiet = True
        self.events.append("quiet")
        try:
            yield
        finally:
            self.quiet = False
            self.events.append("wake")

    def _history_snapshot(self):
        assert self.busy and not self.quiet
        self.events.append("snapshot")
        if self.snapshot_error:
            raise self.snapshot_error
        return ("owned-part", {"ExistingBoss"})

    def _roll_back(self, snapshot):
        assert self.busy and not self.quiet
        self.events.append(("rollback", snapshot))
        return self.left


def test_snapshot_and_mutation_keep_command_mode_and_restore_ui():
    session = Session()
    def mutate(value):
        assert session.busy and session.quiet
        session.events.append("mutate")
        return value
    assert run_guarded(session, mutate, value={"ok": True}) == {"ok": True}
    assert session.events == ["reattach", ("busy", True), "snapshot", "quiet", "mutate", "wake", ("busy", False)]
    assert not session.busy and not session.quiet


@pytest.mark.parametrize("error", [SolidWorksError("CAD failed"), RuntimeError("unexpected")])
def test_failure_retains_snapshot_rollback_after_ui_restore(error):
    session = Session()
    def fail():
        raise error
    with pytest.raises(type(error), match=str(error)):
        run_guarded(session, fail)
    assert ("rollback", ("owned-part", {"ExistingBoss"})) in session.events
    assert not session.busy and not session.quiet


def test_snapshot_failure_never_mutates_and_restores_both_flags():
    session = Session()
    session.snapshot_error = RuntimeError("snapshot unavailable")
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        run_guarded(session, lambda: pytest.fail("mutation must not run"))
    assert ("rollback", None) in session.events
    assert not session.busy and not session.quiet


def test_existing_command_mode_is_not_cleared():
    session = Session(busy=True)
    assert run_guarded(session, lambda: 42) == 42
    assert session.busy and not session.quiet
    assert not any(isinstance(event, tuple) and event[0] == "busy" for event in session.events)


def test_rollback_remainder_retains_original_failure_evidence():
    session = Session()
    session.left = ["BrokenSketch"]
    def fail():
        raise SolidWorksError("original failure")
    with pytest.raises(SolidWorksError, match="original failure.*BrokenSketch") as caught:
        run_guarded(session, fail)
    assert str(caught.value.__cause__) == "original failure"
    assert not session.busy and not session.quiet


def test_rollback_error_still_restores_command_flag():
    session = Session()
    def bad_rollback(snapshot):
        raise RuntimeError("rollback unavailable")
    session._roll_back = bad_rollback
    def fail():
        raise ValueError("bad feature")
    with pytest.raises(RuntimeError, match="rollback unavailable"):
        run_guarded(session, fail)
    assert not session.busy and not session.quiet


class ColdSession(Session):
    def __init__(self):
        super().__init__()
        self._sw = None
        self.connect_error = None
        self.creation_error = None

    def _ensure(self):
        self.events.append("connect")
        if self.connect_error:
            raise self.connect_error
        self._sw = object()

    def _command_in_progress(self, flag):
        if self._sw is None:
            return None
        return super()._command_in_progress(flag)

    def new_part(self):
        assert self._sw is not None and self.busy and self.quiet
        self.events.append("NewDocument")
        if self.creation_error:
            raise self.creation_error
        return {"ok": True}


def test_first_new_part_attaches_before_command_mode_and_creation():
    session = ColdSession()
    assert run_guarded(session, session.new_part) == {"ok": True}
    assert session.events[:4] == ["reattach", "connect", ("busy", True), "snapshot"]
    assert "NewDocument" in session.events
    assert not session.busy and not session.quiet


def test_first_connection_failure_does_not_create_document():
    session = ColdSession()
    session.connect_error = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        run_guarded(session, session.new_part)
    assert session.events == ["reattach", "connect"]
    assert not session.busy and not session.quiet


def test_first_document_failure_still_restores_command_and_ui():
    session = ColdSession()
    session.creation_error = SolidWorksError("template failed")
    with pytest.raises(SolidWorksError, match="template failed"):
        run_guarded(session, session.new_part)
    assert not session.busy and not session.quiet
