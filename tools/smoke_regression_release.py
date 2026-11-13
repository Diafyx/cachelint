"""Exercise an installed package against the maintained regression CI example."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from cachelint.cli import main


def smoke(example: Path) -> None:
    """Verify baseline creation, safe exports, gate failure, and invalid input."""
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
        work = Path(folder)
        reviewed_report = work / "reviewed-report.json"
        baseline = work / "baseline.json"
        current_report = work / "current-report.json"
        result_file = work / "regression.json"
        summary_file = work / "regression.md"
        assert (
            main(
                [
                    "corpus",
                    str(example / "reviewed-requests.jsonl"),
                    "--format",
                    "json",
                    "--output",
                    str(reviewed_report),
                ]
            )
            == 0
        )
        assert (
            main(
                ["baseline", "create", str(reviewed_report), "--output", str(baseline)]
            )
            == 0
        )
        assert (
            baseline.read_bytes() == (example / "reviewed-baseline.json").read_bytes()
        )
        assert (
            main(
                [
                    "corpus",
                    str(example / "current-requests.jsonl"),
                    "--format",
                    "json",
                    "--output",
                    str(current_report),
                ]
            )
            == 0
        )
        check = [
            "baseline",
            "check",
            str(baseline),
            str(current_report),
            "--policy",
            str(example / "policy.json"),
            "--format",
            "json",
            "--output",
            str(result_file),
            "--summary-output",
            str(summary_file),
        ]
        assert main(check) == 0
        assert json.loads(result_file.read_text(encoding="utf-8"))["status"] == "pass"
        assert "**Policy status:** pass" in summary_file.read_text(encoding="utf-8")

        changed = work / "changed.jsonl"
        text = (example / "current-requests.jsonl").read_text(encoding="utf-8")
        changed.write_text(
            text.replace("SYNTHETIC_EXAMPLE_A", "SYNTHETIC_CHANGED", 1),
            encoding="utf-8",
        )
        assert (
            main(
                [
                    "corpus",
                    str(changed),
                    "--format",
                    "json",
                    "--output",
                    str(current_report),
                ]
            )
            == 0
        )
        assert main(check) == 3
        assert json.loads(result_file.read_text(encoding="utf-8"))["status"] == "fail"
        assert "**Policy status:** fail" in summary_file.read_text(encoding="utf-8")
        for safe_file in (result_file, summary_file):
            output = safe_file.read_text(encoding="utf-8")
            assert "SYNTHETIC_EXAMPLE_INSTRUCTION" not in output
            assert "SYNTHETIC_CHANGED" not in output

        invalid = work / "invalid.jsonl"
        invalid.write_text("{", encoding="utf-8")
        missing = work / "invalid-report.json"
        assert (
            main(["corpus", str(invalid), "--format", "json", "--output", str(missing)])
            == 2
        )
        assert not missing.exists()
    print("Installed regression workflow smoke passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_regression_release.py EXAMPLE_DIR")
    smoke(Path(sys.argv[1]))
