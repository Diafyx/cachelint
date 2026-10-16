"""Validate the built wheel and sdist without extracting either archive."""

from __future__ import annotations

import ast
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path


def source_version(project_root: Path) -> str:
    """Read the one version literal used by both runtime and build metadata."""
    source = (project_root / "src" / "cachelint" / "_version.py").read_text(
        encoding="utf-8"
    )
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
    raise ValueError("No literal __version__ found in src/cachelint/_version.py")


def inspect(dist_dir: Path) -> None:
    """Check the exact two expected artifacts and their public contents."""
    project_root = Path(__file__).resolve().parents[1]
    version = source_version(project_root)
    wheels = list(dist_dir.glob("cachelint-*.whl"))
    sdists = list(dist_dir.glob("cachelint-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Expected exactly one cachelint wheel and one sdist")
    if not wheels[0].name.startswith(f"cachelint-{version}-"):
        raise ValueError("Wheel filename does not match the source version")
    if sdists[0].name != f"cachelint-{version}.tar.gz":
        raise ValueError("Sdist filename does not match the source version")

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        metadata_paths = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("Wheel must contain one METADATA file")
        metadata = Parser().parsestr(wheel.read(metadata_paths[0]).decode("utf-8"))
        expected_fields = {
            "Name": "cachelint",
            "Version": version,
            "Requires-Python": ">=3.11",
            "License-Expression": "Apache-2.0",
        }
        for field, expected in expected_fields.items():
            if metadata.get(field) != expected:
                raise ValueError(f"Wheel {field} is not {expected!r}")
        if metadata.get_all("Requires-Dist"):
            raise ValueError("Core wheel unexpectedly declares runtime dependencies")
        required_wheel = {
            "cachelint/__init__.py",
            "cachelint/_ingest.py",
            "cachelint/_version.py",
            "cachelint/cli.py",
            "cachelint/py.typed",
        }
        if not required_wheel.issubset(names):
            raise ValueError("Wheel is missing package files")
        if not any(name.endswith("/LICENSE") for name in names):
            raise ValueError("Wheel is missing LICENSE")
        entry_points = [
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(entry_points) != 1 or "cachelint = cachelint.cli:main" not in wheel.read(
            entry_points[0]
        ).decode("utf-8"):
            raise ValueError("Wheel is missing the cachelint CLI entry point")
        if any(".tmp/" in name for name in names):
            raise ValueError("Wheel includes local .tmp files")

    with tarfile.open(sdists[0], mode="r:gz") as sdist:
        names = {name.split("/", 1)[1] for name in sdist.getnames() if "/" in name}
        required_sdist = {
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/cachelint/__init__.py",
            "src/cachelint/_ingest.py",
            "src/cachelint/_version.py",
            "src/cachelint/cli.py",
            "src/cachelint/py.typed",
        }
        if not required_sdist.issubset(names):
            raise ValueError("Sdist is missing source or metadata files")
        if any(".tmp/" in name for name in names):
            raise ValueError("Sdist includes local .tmp files")

    print(f"Validated wheel and sdist for cachelint {version}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/inspect_artifacts.py DIST_DIR")
    inspect(Path(sys.argv[1]))
