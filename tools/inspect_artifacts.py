"""Validate the built wheel and sdist without extracting either archive."""

from __future__ import annotations

import ast
import json
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
        dependencies = metadata.get_all("Requires-Dist", [])
        if (
            set(metadata.get_all("Provides-Extra", []))
            != {"tokenizer", "sdk-anthropic", "sdk-openai"}
            or len(dependencies) != 3
            or not any(
                item.startswith("tiktoken>=0.14") and 'extra == "tokenizer"' in item
                for item in dependencies
            )
            or not any(
                item.startswith("anthropic==1.8.0")
                and 'extra == "sdk-anthropic"' in item
                for item in dependencies
            )
            or not any(
                item.startswith("openai==3.37.0") and 'extra == "sdk-openai"' in item
                for item in dependencies
            )
        ):
            raise ValueError("Wheel optional dependency metadata differs")
        required_wheel = {
            "cachelint/__init__.py",
            "cachelint/_compare.py",
            "cachelint/_corpus.py",
            "cachelint/_estimate.py",
            "cachelint/_evidence.py",
            "cachelint/_ingest.py",
            "cachelint/_observed.py",
            "cachelint/_usage.py",
            "cachelint/recording.py",
            "cachelint/_version.py",
            "cachelint/cli.py",
            "cachelint/report.py",
            "cachelint/py.typed",
            "cachelint/cache_rules.json",
            "cachelint/corpus_report_v1.schema.json",
            "cachelint/corpus_report_v2.schema.json",
            "cachelint/usage_sidecar_v1.schema.json",
            "cachelint/usage_report_v1.schema.json",
        }
        if not required_wheel.issubset(names):
            raise ValueError("Wheel is missing package files")
        schema = json.loads(wheel.read("cachelint/corpus_report_v1.schema.json"))
        if schema.get("properties", {}).get("schema_version") != {"const": 1}:
            raise ValueError("Wheel corpus report schema is invalid")
        observed_schema = json.loads(
            wheel.read("cachelint/corpus_report_v2.schema.json")
        )
        if observed_schema.get("properties", {}).get("schema_version") != {"const": 2}:
            raise ValueError("Wheel observed corpus report schema is invalid")
        usage_schema = json.loads(wheel.read("cachelint/usage_sidecar_v1.schema.json"))
        if usage_schema.get("properties", {}).get("schema_version") != {"const": 1}:
            raise ValueError("Wheel usage sidecar schema is invalid")
        usage_report_schema = json.loads(
            wheel.read("cachelint/usage_report_v1.schema.json")
        )
        if usage_report_schema.get("properties", {}).get("schema_version") != {
            "const": 1
        }:
            raise ValueError("Wheel usage report schema is invalid")
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
            "MANIFEST.in",
            "pyproject.toml",
            "docs/recording.md",
            "docs/usage.md",
            "docs/corpus.md",
            "docs/design/0004-cache-usage-sidecar-contract.md",
            "docs/design/0005-observed-usage-join-contract.md",
            "docs/design/0006-observed-corpus-findings.md",
            "docs/releases/0.2.0a2.md",
            "docs/releases/0.2.0a1.md",
            "docs/releases/0.1.0a3.md",
            "docs/design/0003-private-sdk-recording-contract.md",
            "src/cachelint/__init__.py",
            "src/cachelint/_compare.py",
            "src/cachelint/_corpus.py",
            "src/cachelint/_estimate.py",
            "src/cachelint/_evidence.py",
            "src/cachelint/_ingest.py",
            "src/cachelint/_observed.py",
            "src/cachelint/_usage.py",
            "src/cachelint/recording.py",
            "src/cachelint/_version.py",
            "src/cachelint/cli.py",
            "src/cachelint/report.py",
            "src/cachelint/py.typed",
            "src/cachelint/cache_rules.json",
            "src/cachelint/corpus_report_v1.schema.json",
            "src/cachelint/corpus_report_v2.schema.json",
            "src/cachelint/usage_sidecar_v1.schema.json",
            "src/cachelint/usage_report_v1.schema.json",
        }
        if not required_sdist.issubset(names):
            raise ValueError("Sdist is missing source or metadata files")
        schema_file = sdist.extractfile(
            f"cachelint-{version}/src/cachelint/corpus_report_v1.schema.json"
        )
        if schema_file is None or json.load(schema_file).get("properties", {}).get(
            "schema_version"
        ) != {"const": 1}:
            raise ValueError("Sdist corpus report schema is invalid")
        observed_schema_file = sdist.extractfile(
            f"cachelint-{version}/src/cachelint/corpus_report_v2.schema.json"
        )
        if observed_schema_file is None or json.load(observed_schema_file).get(
            "properties", {}
        ).get("schema_version") != {"const": 2}:
            raise ValueError("Sdist observed corpus report schema is invalid")
        usage_schema_file = sdist.extractfile(
            f"cachelint-{version}/src/cachelint/usage_sidecar_v1.schema.json"
        )
        if usage_schema_file is None or json.load(usage_schema_file).get(
            "properties", {}
        ).get("schema_version") != {"const": 1}:
            raise ValueError("Sdist usage sidecar schema is invalid")
        usage_report_schema_file = sdist.extractfile(
            f"cachelint-{version}/src/cachelint/usage_report_v1.schema.json"
        )
        if usage_report_schema_file is None or json.load(usage_report_schema_file).get(
            "properties", {}
        ).get("schema_version") != {"const": 1}:
            raise ValueError("Sdist usage report schema is invalid")
        if any(".tmp/" in name for name in names):
            raise ValueError("Sdist includes local .tmp files")

    print(f"Validated wheel and sdist for cachelint {version}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/inspect_artifacts.py DIST_DIR")
    inspect(Path(sys.argv[1]))
