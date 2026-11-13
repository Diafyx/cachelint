"""The maintained synthetic CI example is reproducible and safe to export."""

from __future__ import annotations

import json
from pathlib import Path

from cachelint.cli import main

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "regression-ci"


def test_example_baseline_and_gate(tmp_path: Path) -> None:
    reviewed_report = tmp_path / "reviewed-report.json"
    baseline = tmp_path / "baseline.json"
    current_report = tmp_path / "current-report.json"
    gate = tmp_path / "regression.json"
    summary = tmp_path / "regression.md"
    assert (
        main(
            [
                "corpus",
                str(EXAMPLE / "reviewed-requests.jsonl"),
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
            [
                "baseline",
                "create",
                str(reviewed_report),
                "--output",
                str(baseline),
            ]
        )
        == 0
    )
    assert baseline.read_bytes() == (EXAMPLE / "reviewed-baseline.json").read_bytes()
    assert (
        main(
            [
                "corpus",
                str(EXAMPLE / "current-requests.jsonl"),
                "--format",
                "json",
                "--output",
                str(current_report),
            ]
        )
        == 0
    )
    command = [
        "baseline",
        "check",
        str(baseline),
        str(current_report),
        "--policy",
        str(EXAMPLE / "policy.json"),
        "--format",
        "json",
        "--output",
        str(gate),
        "--summary-output",
        str(summary),
    ]
    assert main(command) == 0
    result = json.loads(gate.read_text(encoding="utf-8"))
    assert result["status"] == "pass"
    assert result["summary"]["matched_cohort_count"] == 2
    assert result["summary"]["measurement_count"] == 4
    for safe_output in (gate, summary):
        assert "SYNTHETIC_EXAMPLE_INSTRUCTION" not in safe_output.read_text(
            encoding="utf-8"
        )

    altered = tmp_path / "altered.jsonl"
    content = (EXAMPLE / "current-requests.jsonl").read_text(encoding="utf-8")
    altered.write_text(
        content.replace("SYNTHETIC_EXAMPLE_A", "SYNTHETIC_CHANGED", 1), encoding="utf-8"
    )
    assert (
        main(
            [
                "corpus",
                str(altered),
                "--format",
                "json",
                "--output",
                str(current_report),
            ]
        )
        == 0
    )
    assert main(command) == 3
    assert json.loads(gate.read_text(encoding="utf-8"))["status"] == "fail"
    assert "**Policy status:** fail" in summary.read_text(encoding="utf-8")
