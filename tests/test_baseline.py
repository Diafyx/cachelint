"""Versioned baseline projection, privacy, CLI, and atomic file behavior."""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pytest

from cachelint import CorpusUsageTrace, InvalidRequestError, analyze_observed_corpus
from cachelint.baseline import (
    _matches,
    baseline_from_report,
    load_baseline,
    load_corpus_report,
    render_baseline_json,
    render_baseline_text,
)
from cachelint.cli import main
from cachelint.report import corpus_report

FIXTURES = Path(__file__).parent / "fixtures"
CANARY = "PRIVATE_BASELINE_CANARY_MODEL_PROMPT_PATH_HASH"


def _v1() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURES / "corpus-report-v1.json").read_text(encoding="utf-8")),
    )


def _v2(tmp_path: Path) -> dict[str, Any]:
    requests = tmp_path / "private.jsonl"
    sidecar = tmp_path / "private.jsonl.usage.jsonl"
    bodies = [
        {
            "model": CANARY,
            "system": CANARY,
            "messages": [{"role": "user", "content": CANARY + suffix}],
        }
        for suffix in ("A", "B")
    ]
    first = (json.dumps(bodies[0]) + "\n").encode("utf-8")
    requests.write_bytes(first + (json.dumps(bodies[1]) + "\n").encode("utf-8"))
    sidecar.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "attempt_id": index,
                    "capture_status": "written",
                    "request_offset": offset,
                    "usage_status": "reported",
                    "usage": {"cache_read_input_tokens": 0},
                }
            )
            + "\n"
            for index, offset in ((1, 0), (2, len(first)))
        ),
        encoding="utf-8",
    )
    report = cast(
        dict[str, Any],
        corpus_report(
            analyze_observed_corpus(
                requests,
                usage_traces=[CorpusUsageTrace("anthropic", requests, sidecar)],
            )
        ),
    )
    assert report["observed_findings"]
    report["observed_findings"][0]["observation"] = CANARY
    report["observed_findings"][0]["plausible_causes"] = [CANARY]
    report["observed_findings"][0]["actions"] = [CANARY]
    return report


def test_v1_create_inspect_determinism_and_privacy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _v1()
    source = tmp_path / "report.json"
    destination = tmp_path / "baseline.json"
    source.write_text(json.dumps(report, indent=1), encoding="utf-8")
    assert main(["baseline", "create", str(source), "--output", str(destination)]) == 0
    assert capsys.readouterr() == ("", "")
    first = destination.read_bytes()
    assert first == (FIXTURES / "baseline-v1.json").read_bytes()
    document = json.loads(first)
    assert document["schema_version"] == 1
    assert document["source_report_schema_version"] == 1
    assert document["summary"] == {
        "file_count": 2,
        "request_count": 4,
        "cohort_count": 2,
        "pattern_count": 2,
        "observed_finding_count": 0,
    }
    assert all(item["usage"] is None for item in document["cohorts"])
    assert document["observed_findings"] == []
    assert first.endswith(b"\n") and b"\r" not in first
    assert CANARY.encode() not in first
    assert load_baseline(destination) == document
    assert main(["baseline", "inspect", str(destination)]) == 0
    assert "Baseline v1" in capsys.readouterr().out
    assert main(["baseline", "inspect", str(destination), "--format", "json"]) == 0
    assert capsys.readouterr().out.encode() == first

    report["groups"].reverse()
    report["patterns"].reverse()
    source.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    assert main(["baseline", "create", str(source), "--output", str(destination)]) == 0
    assert capsys.readouterr() == ("", "")
    assert destination.read_bytes() == first
    assert not list(tmp_path.glob(".cachelint-report-*.tmp"))


def test_v2_projection_omits_narratives_and_private_values(tmp_path: Path) -> None:
    report = _v2(tmp_path)
    baseline = baseline_from_report(report)
    output = render_baseline_json(baseline)
    assert baseline["source_report_schema_version"] == 2
    assert baseline["cohorts"][0]["usage"]["cache_read_zero_count"] == 2
    assert baseline["summary"]["observed_finding_count"] > 0
    assert "matching_prefix_no_read" in output or "divergent_prefix_no_read" in output
    for secret in (CANARY, str(tmp_path), "claude-sonnet"):
        assert secret not in output
        assert secret not in render_baseline_text(baseline)
    assert json.loads(
        files("cachelint").joinpath("baseline_v1.schema.json").read_text()
    )["properties"]["schema_version"] == {"const": 1}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(schema_version=3),
        lambda item: item.update(secret=CANARY),
        lambda item: item["summary"].update(request_count=-1),
        lambda item: item["summary"].update(cohort_count=999),
        lambda item: item["groups"][0].update(index=9),
        lambda item: item["groups"][0]["comparison"].update(
            finding_codes=["PRIVATE_CODE"]
        ),
        lambda item: item["patterns"][0].update(path="messages[0].PRIVATE_SECRET"),
        lambda item: item["patterns"][0].update(group_index=99),
    ],
)
def test_invalid_and_stale_report_never_write_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutate: Any,
) -> None:
    report = _v1()
    mutate(report)
    source = tmp_path / "report.json"
    destination = tmp_path / "baseline.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    destination.write_text("keep", encoding="utf-8")
    assert main(["baseline", "create", str(source), "--output", str(destination)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert CANARY not in output.err
    assert destination.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "change",
    [
        lambda item: item.update(schema_version=2),
        lambda item: item.update(extra=CANARY),
        lambda item: item["summary"].update(request_count=99),
        lambda item: item["cohorts"][0].update(provider="PRIVATE_PROVIDER"),
        lambda item: item["patterns"][0].update(path="PRIVATE_PATH"),
        lambda item: item["patterns"][0].update(cohort_index=99),
    ],
)
def test_invalid_and_stale_baseline_rejected(
    tmp_path: Path, change: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    document = deepcopy(baseline_from_report(_v1()))
    change(document)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["baseline", "inspect", str(path)]) == 2
    assert capsys.readouterr().out == ""


def test_duplicate_keys_nonfinite_and_symlink_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_corpus_report(duplicate)
    duplicate.write_text('{"schema_version":NaN}', encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_corpus_report(duplicate)
    linked = tmp_path / "link.json"
    try:
        linked.symlink_to(duplicate)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(InvalidRequestError):
        load_corpus_report(linked)


def test_interrupted_write_preserves_old_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "report.json"
    source.write_text(json.dumps(_v1()), encoding="utf-8")
    destination = tmp_path / "baseline.json"
    destination.write_text("old baseline", encoding="utf-8")

    def fail_replace(*args: object) -> None:
        raise OSError("PRIVATE_INTERRUPTED_WRITE")

    monkeypatch.setattr("cachelint.cli.os.replace", fail_replace)
    assert main(["baseline", "create", str(source), "--output", str(destination)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "PRIVATE_INTERRUPTED_WRITE" not in output.err
    assert destination.read_text(encoding="utf-8") == "old baseline"
    assert not list(tmp_path.glob(".cachelint-report-*.tmp"))


def test_empty_corpus_and_output_guards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "empty.json"
    report.write_bytes((FIXTURES / "corpus-empty-v1.json").read_bytes())
    document = baseline_from_report(load_corpus_report(report))
    assert document["cohorts"] == []
    assert document["patterns"] == []
    assert "cohorts: 0" in render_baseline_text(document)
    assert main(["baseline", "create", str(report), "--output", str(report)]) == 2
    assert "must not replace an input" in capsys.readouterr().err
    assert (
        main(["baseline", "create", str(report), "--output", str(tmp_path / "x.jsonl")])
        == 2
    )
    assert "JSONL extension" in capsys.readouterr().err


def test_input_file_bounds_and_json_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(InvalidRequestError, match="regular file"):
        load_corpus_report(tmp_path / "absent.json")
    source = tmp_path / "report.json"
    source.write_text("[]", encoding="utf-8")
    with pytest.raises(InvalidRequestError, match="must be an object"):
        load_corpus_report(source)
    monkeypatch.setattr("cachelint.baseline.MAX_DOCUMENT_BYTES", 1)
    with pytest.raises(InvalidRequestError, match="exceeds 32 MiB"):
        load_corpus_report(source)


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("x", {"$ref": "https://wrong.example/schema"}),
        (None, {"oneOf": [{"type": "null"}, {"type": "null"}]}),
        (3, {"const": 4}),
        (42, {"format": "date"}),
        ("2026-99-99", {"format": "date"}),
        ("2026-W46-4", {"format": "date"}),
        (42, {"format": "uri"}),
        ("not-a-uri", {"format": "uri"}),
        ("http://[", {"format": "uri"}),
        ({}, {"type": "object", "required": ["missing"]}),
    ],
)
def test_schema_guard_rejects_malformed_values(value: object, rule: Any) -> None:
    assert not _matches(value, rule, {})


def test_schema_guard_accepts_valid_formats_and_references() -> None:
    defs = {"integer": {"type": "integer"}}
    assert _matches(4, {"$ref": "#/$defs/integer"}, defs)
    assert not _matches(4, {"$ref": "#/$defs/missing"}, defs)
    assert _matches("2026-11-12", {"format": "date"}, {})
    assert _matches("https://example.com/", {"format": "uri"}, {})


def test_single_request_has_no_advisory_estimate(tmp_path: Path) -> None:
    request = tmp_path / "one.jsonl"
    request.write_text(
        json.dumps({"model": "private-model", "system": CANARY, "messages": []}) + "\n",
        encoding="utf-8",
    )
    from cachelint import analyze_corpus

    baseline = baseline_from_report(corpus_report(analyze_corpus(request)))
    assert baseline["cohorts"][0]["prefix_estimate"] is None
    assert CANARY not in render_baseline_json(baseline)


def test_inconsistent_cohort_and_v1_observed_finding_rejected(tmp_path: Path) -> None:
    baseline = baseline_from_report(_v1())
    baseline["cohorts"][0]["paired_count"] = 3
    with pytest.raises(InvalidRequestError, match="cohort counts"):
        render_baseline_json(baseline)

    baseline = baseline_from_report(_v1())
    observed = baseline_from_report(_v2(tmp_path))["observed_findings"][0]
    observed["cohort_index"] = 1
    baseline["observed_findings"] = [observed]
    baseline["summary"]["observed_finding_count"] = 1
    with pytest.raises(InvalidRequestError, match="request-only baseline"):
        render_baseline_json(baseline)

    baseline = baseline_from_report(_v2(tmp_path))
    baseline["cohorts"][0]["usage"]["request_count"] = 99
    with pytest.raises(InvalidRequestError, match="usage counts"):
        render_baseline_json(baseline)
