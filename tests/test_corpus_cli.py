"""Corpus CLI report snapshots, privacy, atomic output, and error behavior."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path

import pytest

from cachelint.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _write(path: Path, *bodies: dict[str, object]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            "".join(json.dumps(body, sort_keys=True) + "\n" for body in bodies)
        )
    return path


def _anthropic(model: str, system: str) -> dict[str, object]:
    return {
        "model": model,
        "system": system,
        "messages": [{"role": "user", "content": "PRIVATE_CANARY_PROMPT"}],
    }


def _openai(model: str, content: str) -> dict[str, object]:
    return {
        "model": model,
        "response_format": {"type": "text"},
        "messages": [{"role": "user", "content": content}],
    }


def test_json_golden_multiple_cohorts_and_ranking_tie(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    later = _write(
        tmp_path / "z.jsonl",
        _anthropic("PRIVATE_MODEL_A", "PRIVATE_SYSTEM_B"),
        _openai("PRIVATE_MODEL_B", "PRIVATE_MESSAGE_B"),
    )
    earlier = _write(
        tmp_path / "a.jsonl",
        _anthropic("PRIVATE_MODEL_A", "PRIVATE_SYSTEM_A"),
        _openai("PRIVATE_MODEL_B", "PRIVATE_MESSAGE_A"),
    )
    assert main(["corpus", str(later), str(earlier), "--format", "json"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (FIXTURES / "corpus-report-v1.json").read_text(
        encoding="utf-8"
    )
    assert main(["corpus", str(earlier), str(later), "--format", "json"]) == 0
    assert capsys.readouterr().out == output.out
    for secret in (
        "PRIVATE_CANARY_PROMPT",
        "PRIVATE_MODEL_A",
        "PRIVATE_MODEL_B",
        "PRIVATE_SYSTEM_A",
        "PRIVATE_MESSAGE_A",
        str(tmp_path),
    ):
        assert secret not in output.out
    document = json.loads(output.out)
    assert [
        (item["rank"], item["group_index"], item["category"])
        for item in document["patterns"]
    ] == [
        (1, 2, "messages"),
        (2, 1, "system"),
    ]


def test_empty_corpus_json_and_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert main(["corpus", str(empty), "--format", "json"]) == 0
    assert capsys.readouterr().out == (FIXTURES / "corpus-empty-v1.json").read_text(
        encoding="utf-8"
    )
    assert main(["corpus", str(empty)]) == 0
    output = capsys.readouterr().out
    assert "Requests: 0\nCohorts: 0\nRanked patterns: 0" in output
    assert "No request cohorts found." in output


def test_text_report_and_explicit_atomic_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(
        tmp_path / "source.jsonl",
        _anthropic("demo", "first"),
        _anthropic("demo", "second"),
    )
    destination = tmp_path / "report.json"
    destination.write_text("old report", encoding="utf-8")
    assert (
        main(["corpus", str(source), "--format", "json", "--output", str(destination)])
        == 0
    )
    assert capsys.readouterr() == ("", "")
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["summary"]["provider_reported_cache_status"] == "unavailable"
    assert report["patterns"][0]["path"] == "system"
    assert not list(tmp_path.glob(".cachelint-report-*.tmp"))
    assert main(["corpus", str(source), "--output", str(tmp_path / "report.txt")]) == 0
    assert "#1 cohort 1: system system" in (tmp_path / "report.txt").read_text(
        encoding="utf-8"
    )


def test_invalid_input_never_replaces_existing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "bad.jsonl"
    invalid.write_text(
        '{"model":"demo","system":"PRIVATE_CANARY"}\n{', encoding="utf-8"
    )
    destination = tmp_path / "existing.json"
    destination.write_text("keep this", encoding="utf-8")
    assert (
        main(["corpus", str(invalid), "--format", "json", "--output", str(destination)])
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "PRIVATE_CANARY" not in output.err
    assert destination.read_text(encoding="utf-8") == "keep this"
    assert not list(tmp_path.glob(".cachelint-report-*.tmp"))


def test_reject_input_overwrite_and_jsonl_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "source.jsonl", _anthropic("demo", "first"))
    assert main(["corpus", str(source), "--output", str(source)]) == 2
    assert "JSONL extension" in capsys.readouterr().err
    direct = _write(tmp_path / "direct.json", _anthropic("demo", "first"))
    assert main(["corpus", str(direct), "--output", str(direct)]) == 2
    assert "must not replace an input" in capsys.readouterr().err
    assert main(["corpus", str(source), "--output", str(tmp_path / "out.jsonl")]) == 2
    assert "JSONL extension" in capsys.readouterr().err


def test_output_failure_cleans_temporary_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.jsonl", _anthropic("demo", "first"))
    destination = tmp_path / "report.json"
    destination.write_text("original", encoding="utf-8")

    def fail_replace(*args: object) -> None:
        raise OSError("PRIVATE_CANARY_FAILURE")

    monkeypatch.setattr("cachelint.cli.os.replace", fail_replace)
    assert main(["corpus", str(source), "--output", str(destination)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot write report" in output.err
    assert "PRIVATE_CANARY_FAILURE" not in output.err
    assert destination.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".cachelint-report-*.tmp"))


def test_symlink_output_is_rejected_before_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.jsonl", _anthropic("demo", "first"))
    destination = tmp_path / "report.json"
    original = Path.is_symlink

    def pretend_symlink(path: Path) -> bool:
        return path == destination or original(path)

    monkeypatch.setattr(Path, "is_symlink", pretend_symlink)
    assert main(["corpus", str(source), "--output", str(destination)]) == 2
    assert "must not be a symlink" in capsys.readouterr().err
    assert not destination.exists()


def test_single_request_report_has_explicit_insufficient_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "one.jsonl", _anthropic("demo", "first"))
    assert main(["corpus", str(source), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["groups"][0]["comparison"]["prompt_status"] == "insufficient_data"
    assert report["groups"][0]["comparison"]["first_divergence"] is None
    assert report["groups"][0]["comparison"]["prefix_estimate"] is None
    assert report["patterns"] == []


def test_structural_pattern_text_has_no_offset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reference = _anthropic("demo", "first")
    reference["tools"] = [{"name": "private-one"}]
    changed = _anthropic("demo", "first")
    changed["tools"] = [{"name": "private-two"}]
    source = _write(tmp_path / "tools.jsonl", reference, changed)
    assert main(["corpus", str(source)]) == 0
    output = capsys.readouterr().out
    assert "tools tools[0] (object_changed)" in output
    assert "text offsets" not in output
    assert "private-one" not in output


def test_invalid_limits_and_missing_output_parent_are_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "source.jsonl", _anthropic("demo", "first"))
    assert main(["corpus", str(source), "--max-requests", "0"]) == 2
    assert "positive integers" in capsys.readouterr().err
    assert (
        main(
            ["corpus", str(source), "--output", str(tmp_path / "missing" / "out.json")]
        )
        == 2
    )
    assert "cannot write report" in capsys.readouterr().err


def test_json_schema_is_packaged_with_required_v1_fields() -> None:
    schema = json.loads(
        files("cachelint")
        .joinpath("corpus_report_v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert set(schema["required"]) == {
        "schema_version",
        "report_type",
        "summary",
        "groups",
        "patterns",
    }
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["pattern"]["additionalProperties"] is False
    safe_path = re.compile(schema["$defs"]["safe_path"]["pattern"])
    for path in ("tools[0]", "system", "system[0].text", "messages[0].content"):
        assert safe_path.fullmatch(path)
    assert not safe_path.fullmatch("messages[0].PRIVATE_CANARY")
