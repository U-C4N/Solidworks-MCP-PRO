"""Task-owned output storage and a cross-process CAD-operation lease."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def default_workspace() -> Path:
    return Path(os.environ.get("SW_MCP_PRO_WORKSPACE", str(Path.home() / "Documents" / "Solidworks-MCP-PRO"))).resolve()


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, tuple[Path, str]] = {}

    def allocate(self, suffix: str) -> Path:
        return self.root / (uuid4().hex + suffix)

    def register(self, path: Path, mime: str) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Only files generated in the output workspace can be published")
        key = uuid4().hex + resolved.suffix.lower()
        self.items[key] = resolved, mime
        return "solidworks-pro://artifact/" + key

    def read(self, key: str) -> tuple[bytes, str]:
        if key not in self.items:
            raise ValueError("Unknown artifact; generate it in this server session first")
        path, mime = self.items[key]
        if not path.resolve(strict=True).is_relative_to(self.root):
            raise ValueError("Artifact moved outside workspace")
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Artifact too large for an MCP resource; open the local file")
        return path.read_bytes(), mime


class CadBusyError(RuntimeError):
    pass


class CadLease:
    """Nonblocking advisory lock shared by PRO instances, not unrelated MCP servers."""
    def __init__(self, path: Path | None = None):
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache")))
        self.path = path or base / "Solidworks-MCP-PRO" / "cad.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise CadBusyError("Another PRO process is using CAD. Check its status before retrying.") from exc
        return self

    def __exit__(self, *args):
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None
