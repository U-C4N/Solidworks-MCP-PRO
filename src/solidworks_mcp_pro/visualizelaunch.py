"""Validate local project paths before native Visualize LaunchVisualize calls.

This module does not launch processes, execute registry commands or use UI
automation. Opening is owned by visualize_bridge on the existing COM worker.
"""
from __future__ import annotations

import ntpath
from pathlib import Path
import re


class VisualizeLaunchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _local_absolute(value: str) -> None:
    drive, tail = ntpath.splitdrive(value)
    if (not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("/", "\\"))
            or any(char in value for char in ("\x00", "\r", "\n")) or ":" in tail):
        raise VisualizeLaunchError("INVALID_PROJECT_PATH", "Use an absolute local drive path; UNC/device paths and alternate data streams are not accepted.")


def _drive_type(root: str) -> int:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.GetDriveTypeW
    function.argtypes = [wintypes.LPCWSTR]
    function.restype = wintypes.UINT
    return int(function(root))


def _project_file(path: str) -> Path:
    if not isinstance(path, str):
        raise VisualizeLaunchError("INVALID_PROJECT_PATH", "path must be a string.")
    _local_absolute(path)
    if Path(path).suffix.casefold() != ".svpj":
        raise VisualizeLaunchError("INVALID_PROJECT_PATH", "Only .svpj Visualize project files can be opened.")
    try:
        project = Path(path).resolve(strict=True)
        _local_absolute(str(project))
        if not project.is_file() or project.suffix.casefold() != ".svpj" or project.stat().st_size == 0:
            raise VisualizeLaunchError("INVALID_PROJECT_PATH", "The project must resolve to an existing nonempty .svpj file.")
        # Drive letters can map to network shares. Require local fixed,
        # removable or RAM storage after resolving any junction/symlink.
        if _drive_type(project.anchor) not in (2, 3, 6):
            raise VisualizeLaunchError("INVALID_PROJECT_PATH", "The project must be on a local drive; mapped network drives are not accepted.")
        return project
    except VisualizeLaunchError:
        raise
    except (OSError, ValueError) as exc:
        raise VisualizeLaunchError("INVALID_PROJECT_PATH", f"Cannot read the Visualize project: {exc}") from exc
