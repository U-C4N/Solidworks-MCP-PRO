"""Project path validation only; no process launch, COM or UI calls."""
import sys

import pytest

from solidworks_mcp_pro import visualizelaunch as launch


@pytest.mark.parametrize("path", [
    "relative.svpj", r"C:relative.svpj", r"\rooted.svpj", r"\\server\share\part.svpj",
    r"\\?\C:\part.svpj", r"\\.\C:\part.svpj", r"C:\part.svpj:stream", "C:\\bad\x00.svpj",
])
def test_rejects_nonlocal_or_ambiguous_paths(path):
    with pytest.raises(launch.VisualizeLaunchError, match="absolute local"):
        launch._local_absolute(path)


def test_project_path_must_be_string():
    with pytest.raises(launch.VisualizeLaunchError, match="string"):
        launch._project_file(None)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows filesystem validation")
@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "wrong_suffix", "network"])
def test_project_validation_rejects_unusable_files(tmp_path, monkeypatch, kind):
    path = tmp_path / ("part.exe" if kind == "wrong_suffix" else "part.svpj")
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(b"" if kind == "empty" else b"project")
    monkeypatch.setattr(launch, "_drive_type", lambda root: 4 if kind == "network" else 3)
    with pytest.raises(launch.VisualizeLaunchError):
        launch._project_file(str(path))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows filesystem validation")
def test_accepts_existing_local_project_without_loading_it(tmp_path, monkeypatch):
    project = tmp_path / "Part.SVPJ"
    project.write_bytes(b"project")
    monkeypatch.setattr(launch, "_drive_type", lambda root: 3)
    assert launch._project_file(str(project)) == project
