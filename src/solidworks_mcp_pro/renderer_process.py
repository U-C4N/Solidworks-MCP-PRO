"""Read-only OfflineRenderer process barrier for sequential native renders.

Visualize 2026 checks for any process named OfflineRenderer before its Render,
StartRender and StartRender2 methods. If present it opens a synchronous warning
dialog, even when that previous process exits immediately afterwards. Wait for
cleanup before entering the native API. This never kills or opens a process.
An unrelated application can still start a renderer after the final snapshot;
this barrier prevents observed sequential-job overlap, not external races.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import time


class RendererProcessError(RuntimeError):
    pass


def renderer_processes() -> list[dict]:
    """List exact OfflineRenderer.exe names using Windows Toolhelp snapshots."""
    if os.name != "nt":
        raise RendererProcessError("OfflineRenderer process inspection requires Windows.")

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise RendererProcessError(f"CreateToolhelp32Snapshot failed: Windows error {ctypes.get_last_error()}.")
    result = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            if str(entry.szExeFile).casefold() == "offlinerenderer.exe":
                result.append({"pid": int(entry.th32ProcessID), "name": str(entry.szExeFile)})
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        last_error = ctypes.get_last_error()
        if last_error != 18:  # ERROR_NO_MORE_FILES
            raise RendererProcessError(f"Process enumeration failed: Windows error {last_error}.")
    finally:
        kernel.CloseHandle(snapshot)
    return sorted(result, key=lambda process: process["pid"])


def wait_for_renderer_idle(timeout_seconds: float = 60) -> dict:
    """Wait at most 60 seconds for at least 0.5 seconds of sampled absence.

    Returns ``ok=False`` for busy timeout or enumeration failure. Callers must
    reject native render entry in either case; an unknown process state is not
    treated as idle. The owning CAD lease must remain held during this wait.
    """
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0.5 <= timeout_seconds <= 60):
        raise ValueError("timeout_seconds must be finite and within 0.5..60.")
    started = time.monotonic()
    deadline = started + timeout_seconds
    absent_since = None
    observed_pids = set()
    polls = 0
    processes = []
    while True:
        try:
            processes = renderer_processes()
        except Exception as exc:
            return {"ok": False, "code": "RENDERER_PROCESS_INSPECTION_FAILED", "error": str(exc),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "poll_count": polls, "observed_pids": sorted(observed_pids),
                    "native_render_started": False, "processes_terminated": False}
        polls += 1
        now = time.monotonic()
        observed_pids.update(process["pid"] for process in processes)
        if processes:
            absent_since = None
        elif absent_since is None:
            absent_since = now
        absent_seconds = 0.0 if absent_since is None else now - absent_since
        evidence = {"elapsed_ms": round((now - started) * 1000, 3), "poll_count": polls,
                    "observed_pids": sorted(observed_pids), "remaining_processes": processes,
                    "stable_absence_ms": round(absent_seconds * 1000, 3),
                    "native_render_started": False, "processes_terminated": False}
        if not processes and absent_seconds >= 0.5 and now <= deadline:
            return {"ok": True, "status": "renderer_idle", **evidence}
        if now >= deadline:
            return {"ok": False, "code": "VISUALIZE_RENDERER_BUSY",
                    "error": "OfflineRenderer did not remain absent for 0.5 seconds before the timeout. "
                             "No renderer process was terminated; do not start another native render yet.",
                    **evidence}
        remaining_stability = max(0.0, 0.5 - absent_seconds) if absent_since is not None else 0.1
        time.sleep(min(0.1, deadline - now, remaining_stability))
