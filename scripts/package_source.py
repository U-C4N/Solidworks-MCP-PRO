"""Produce a source ZIP without environments, local logs or user CAD files."""
from pathlib import Path
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    output = root / "dist" / f"solidworks-mcp-pro-{version}-source.zip"
    output.parent.mkdir(exist_ok=True)
    included = ["src", "tests", "scripts", "configs", "docs", "benchmarks", ".github"]
    files = [root / name for name in ["README.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", ".gitignore"]]
    for directory in included:
        files.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
                     and not any(part.endswith(".egg-info") for part in path.parts))
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, "Solidworks-MCP-PRO/" + path.relative_to(root).as_posix())
    print(output)


if __name__ == "__main__":
    main()
