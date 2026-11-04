"""Preserve upstream rollback/UI protection while making the snapshot inexpensive.

Based on solidworks-mcp 0.19.0's MIT-licensed run_guarded implementation.
Only the ordering changes: CommandInProgress covers the history snapshot as
well as the operation. UI suppression still surrounds the operation, as in
upstream. No history or failure checks are removed.
"""
from __future__ import annotations

from solidworks_mcp.errors import SolidWorksError


def run_guarded(session, fn, *args, **kwargs):
    session._reattach_if_gone()
    if (getattr(fn, "__self__", None) is session and getattr(fn, "__name__", None) == "new_part"
            and session._sw is None):
        # new_part starts with _ensure anyway. Attach before setting command
        # mode so the first NewDocument gets the same protection as later ones.
        # Do not eagerly attach for unrelated or file-only tool requests.
        session._ensure()
    busy = session._command_in_progress(True)
    snapshot = None
    try:
        snapshot = session._history_snapshot()
        with session._quiet_ui():
            return fn(*args, **kwargs)
    except SolidWorksError as exc:
        left = session._roll_back(snapshot)
        if left:
            raise SolidWorksError(
                f"{exc} Could not remove what the call added: {', '.join(left)}."
            ) from exc
        raise
    except Exception:
        session._roll_back(snapshot)
        raise
    finally:
        session._command_in_progress(busy)
