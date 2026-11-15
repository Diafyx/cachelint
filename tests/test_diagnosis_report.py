"""Golden, privacy, bounds, and CLI controls for versioned diagnosis reports."""

from __future__ import annotations

import json
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from cachelint import (
    CorpusLimits,
    DiagnosisLimits,
    InvalidRequestError,
    analyze_diagnosis,
    diagnosis_report,
    render_diagnosis_json,
    render_diagnosis_text,
)
from cachelint.cli import main
from cachelint.diagnosis import _INFO

FIXTURES = Path(__file__).parent / "fixtures"
_CANARY = "PRIVATE_DIAGNOSIS_REPORT_CANARY"


def _write(path: Path, *bodies: dict[str, object]) -> Path:
    path.write_text(
        "".join(json.dumps(body, sort_keys=True) + "\n" for body in bodies),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _anthropic(model: str, text: str) -> dict[str, object]:
    return {
        "model": model,
        "system": text,
        "messages": [{"role": "user", "content": _CANARY}],
    }


def _openai(model: str, text: str) -> dict[str, object]:
    return {
        "model": model,
        "response_format": {"type": "text"},
        "messages": [{"role": "user", "content": text}],
    }


def test_golden_ranking_ties_cli_and_python_agree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    later = _write(
        tmp_path / "z.jsonl",
        _anthropic("PRIVATE_MODEL_A", "At 2026-11-15T09:12:13Z"),
        _openai("PRIVATE_MODEL_B", "At 2026-11-15T09:12:13Z"),
    )
    earlier = _write(
        tmp_path / "a.jsonl",
        _anthropic("PRIVATE_MODEL_A", "At 2026-11-14T09:12:13Z"),
        _openai("PRIVATE_MODEL_B", "At 2026-11-14T09:12:13Z"),
    )
    expected = (FIXTURES / "diagnosis-report-v1.json").read_text(encoding="utf-8")
    result = analyze_diagnosis([later, earlier])
    assert render_diagnosis_json(result) == expected
    assert main(["diagnose", str(later), str(earlier), "--format", "json"]) == 0
    assert capsys.readouterr() == (expected, "")
    assert main(["diagnose", str(earlier), str(later), "--format", "json"]) == 0
    assert capsys.readouterr() == (expected, "")
    assert main(["diagnose", str(earlier), str(later)]) == 0
    text = capsys.readouterr().out
    assert text == render_diagnosis_text(result)
    json_output = tmp_path / "diagnosis.json"
    assert (
        main(
            [
                "diagnose",
                str(earlier),
                str(later),
                "--format",
                "json",
                "--output",
                str(json_output),
            ]
        )
        == 0
    )
    assert json_output.read_bytes() == render_diagnosis_json(result).encode("utf-8")
    text_output = tmp_path / "diagnosis.txt"
    assert (
        main(["diagnose", str(earlier), str(later), "--output", str(text_output)]) == 0
    )
    assert text_output.read_bytes() == render_diagnosis_text(result).encode("utf-8")
    assert capsys.readouterr() == ("", "")
    assert "#1 cohort 1: volatile_timestamp (candidate)" in text
    assert "#2 cohort 2: volatile_timestamp (candidate)" in text
    for secret in (_CANARY, "PRIVATE_MODEL", "2026-11-14", str(tmp_path)):
        assert secret not in expected + text + repr(result)
    document = json.loads(expected)
    assert [(item["rank"], item["group_index"]) for item in document["findings"]] == [
        (1, 1),
        (2, 2),
    ]


def test_single_request_empty_input_and_directory_discovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    single = _write(tmp_path / "single.jsonl", _openai("demo", "same"))
    result = analyze_diagnosis(single)
    assert [(item.finding.code, item.finding.path) for item in result.findings] == [
        ("insufficient_data", "request")
    ]
    assert main(["diagnose", str(single), "--format", "json"]) == 0
    assert capsys.readouterr().out == render_diagnosis_json(result)
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert main(["diagnose", str(empty), "--format", "json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["summary"]["request_count"] == 0
    assert document["groups"] == document["findings"] == []
    assert main(["diagnose", str(empty)]) == 0
    assert "No request cohorts found." in capsys.readouterr().out
    directory = tmp_path / "inputs"
    directory.mkdir()
    _write(directory / "first.jsonl", _openai("demo", "before"))
    _write(directory / "second.jsonl", _openai("demo", "after"))
    (directory / "ignored.usage.jsonl").write_text("private", encoding="utf-8")
    assert analyze_diagnosis(directory).request_count == 2


def test_atomic_output_errors_and_bounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(
        tmp_path / "source.jsonl",
        _anthropic("demo", "before"),
        _anthropic("demo", "after"),
    )
    destination = tmp_path / "report.json"
    destination.write_text("old", encoding="utf-8")
    assert (
        main(
            ["diagnose", str(source), "--format", "json", "--output", str(destination)]
        )
        == 0
    )
    assert capsys.readouterr() == ("", "")
    assert json.loads(destination.read_text(encoding="utf-8"))["report_type"] == (
        "cachelint.diagnosis"
    )
    assert not list(tmp_path.glob(".cachelint-report-*.tmp"))
    bad = tmp_path / "PRIVATE_PATH_CANARY.jsonl"
    bad.write_text('{"model":"PRIVATE_MODEL"}\n{', encoding="utf-8")
    assert main(["diagnose", str(bad), "--output", str(destination)]) == 2
    error = capsys.readouterr()
    assert error.out == ""
    assert "PRIVATE_MODEL" not in error.err
    assert "PRIVATE_PATH_CANARY" not in error.err
    assert "line 1" in error.err
    assert destination.read_text(encoding="utf-8").startswith("{")
    direct = _write(tmp_path / "direct.json", _anthropic("demo", "same"))
    assert main(["diagnose", str(direct), "--output", str(direct)]) == 2
    assert "must not replace an input" in capsys.readouterr().err
    assert main(["diagnose", str(source), "--output", str(tmp_path / "out.jsonl")]) == 2
    assert "JSONL extension" in capsys.readouterr().err
    assert main(["diagnose", str(source), "--max-requests", "1"]) == 2
    assert "request limit" in capsys.readouterr().err
    assert main(["diagnose", str(source), "--max-work-units", "1"]) == 2
    assert "work limit" in capsys.readouterr().err
    assert main(["diagnose", str(source), "--max-nodes", "0"]) == 2
    assert "positive integers" in capsys.readouterr().err


def test_python_limits_and_safe_schema_validation(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.jsonl", _anthropic("demo", "before"))
    with pytest.raises(InvalidRequestError, match="corpus byte limit"):
        analyze_diagnosis(source, corpus_limits=CorpusLimits(max_bytes=1))
    with pytest.raises(InvalidRequestError, match="expected diagnosis limits"):
        analyze_diagnosis(source, diagnosis_limits=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="diagnosis segment limit"):
        analyze_diagnosis(source, diagnosis_limits=DiagnosisLimits(max_segments=1))
    result = analyze_diagnosis(source)
    schema = json.loads(
        files("cachelint")
        .joinpath("diagnosis_report_v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["finding"]["additionalProperties"] is False
    finding_properties = schema["$defs"]["finding"]["properties"]
    assert set(finding_properties["code"]["enum"]) == set(_INFO)
    assert set(finding_properties["explanation"]["enum"]) == {
        values[1] for values in _INFO.values()
    }
    assert set(finding_properties["action"]["enum"]) == {
        values[2] for values in _INFO.values()
    }
    assert diagnosis_report(result)["report_type"] == "cachelint.diagnosis"
    with pytest.raises(InvalidRequestError, match="noncontiguous diagnosis group"):
        diagnosis_report(replace(result, groups=(replace(result.groups[0], index=2),)))
    with pytest.raises(InvalidRequestError, match="inconsistent diagnosis request"):
        diagnosis_report(replace(result, request_count=2))
    with pytest.raises(InvalidRequestError, match="noncontiguous diagnosis ranks"):
        diagnosis_report(
            replace(result, findings=(replace(result.findings[0], rank=2),))
        )
    with pytest.raises(InvalidRequestError, match="inconsistent diagnosis cohort"):
        diagnosis_report(
            replace(result, findings=(replace(result.findings[0], group_index=2),))
        )
    with pytest.raises(InvalidRequestError, match="unsupported diagnosis wording"):
        wrong = replace(
            result.findings[0].finding,
            action="Inspect the private source requests and the first divergence.",
        )
        diagnosis_report(
            replace(result, findings=(replace(result.findings[0], finding=wrong),))
        )
    with pytest.raises(InvalidRequestError, match="document does not match"):
        wrong = replace(result.findings[0].finding, action=_CANARY)
        diagnosis_report(
            replace(result, findings=(replace(result.findings[0], finding=wrong),))
        )
    with pytest.raises(InvalidRequestError, match="document does not match"):
        wrong = replace(result.findings[0].finding, path=_CANARY)
        diagnosis_report(
            replace(result, findings=(replace(result.findings[0], finding=wrong),))
        )


def test_existing_corpus_report_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "source.jsonl", _anthropic("demo", "same"))
    assert main(["corpus", str(source), "--format", "json"]) == 0
    before = capsys.readouterr().out
    assert main(["diagnose", str(source), "--format", "json"]) == 0
    capsys.readouterr()
    assert main(["corpus", str(source), "--format", "json"]) == 0
    assert capsys.readouterr().out == before


def test_ambiguous_body_needs_explicit_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "ambiguous.jsonl", {"model": "demo", "messages": []})
    assert main(["diagnose", str(source)]) == 2
    assert "provider" in capsys.readouterr().err
    assert main(["diagnose", str(source), "--provider", "openai"]) == 0
    assert "Cohort 1: openai" in capsys.readouterr().out


def test_schema_accepts_structural_and_opaque_safe_paths(tmp_path: Path) -> None:
    tools = _write(
        tmp_path / "tools.jsonl",
        {
            "model": "demo",
            "system": "rules",
            "messages": [{"role": "user", "content": "same"}],
            "tools": [{"name": "a"}, {"name": "b"}],
        },
        {
            "model": "demo",
            "system": "rules",
            "messages": [{"role": "user", "content": "same"}],
            "tools": [{"name": "b"}, {"name": "a"}],
        },
    )
    assert diagnosis_report(analyze_diagnosis(tools))["findings"][0]["path"] == (
        "tools[0]"
    )
    blocks = _write(
        tmp_path / "blocks.jsonl",
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "first"}]}
            ],
        },
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "second"}]}
            ],
        },
    )
    assert diagnosis_report(analyze_diagnosis(blocks))["findings"][0]["path"] == (
        "messages[0].content[0].text"
    )
    opaque = _write(
        tmp_path / "opaque.jsonl",
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {"role": "user", "content": [{"type": "image", "url": "private-a"}]}
            ],
        },
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {"role": "user", "content": [{"type": "image", "url": "private-b"}]}
            ],
        },
    )
    document = diagnosis_report(analyze_diagnosis(opaque))
    assert document["findings"][0]["code"] == "coverage_limited"
    assert "private-a" not in render_diagnosis_json(analyze_diagnosis(opaque))
