"""Tests for the public command-line entry point."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from cachelint import __version__
from cachelint.cli import main


def test_runtime_version_matches_distribution_metadata() -> None:
    assert __version__ == version("cachelint")


def test_no_arguments_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr() == ("", "")


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "analyze" in output
    assert "diff" in output


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out == f"cachelint {__version__}\n"


def test_unknown_argument(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--unknown"])
    assert error.value.code == 2
    assert "unrecognized arguments: --unknown" in capsys.readouterr().err


def _write_json(path: Path, **changes: object) -> Path:
    body: dict[str, object] = {
        "model": "demo",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "private prompt"}],
        **changes,
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def test_analyze_reports_first_divergence_without_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "requests.jsonl"
    bodies = [
        {
            "model": "demo",
            "system": system,
            "messages": [{"role": "user", "content": "private prompt"}],
            "temperature": temperature,
        }
        for system, temperature in [
            ("You are helpful.", 0),
            ("You are careful.", 1),
        ]
    ]
    path.write_text("\n".join(json.dumps(body) for body in bodies), encoding="utf-8")
    assert main(["analyze", str(path)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "Provider: anthropic" in output.out
    assert "Requests: 2" in output.out
    assert "Prompt status: diverged" in output.out
    assert "First divergence: system (text_changed, offset 8)" in output.out
    assert "Context differences: temperature" in output.out
    assert "prompt_text_changed" in output.out
    assert "helpful" not in output.out
    assert "careful" not in output.out
    assert "private prompt" not in output.out


def test_diff_identical_and_explicit_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        '\ufeff{"model":"demo","messages":[{"role":"user","content":"secret"}]}',
        encoding="utf-8",
    )
    second.write_text(
        '{"messages":[{"content":"secret","role":"user"}],"model":"demo"}',
        encoding="utf-8",
    )
    assert main(["diff", str(first), str(second), "--provider", "openai"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "Provider: openai" in output.out
    assert "Prompt status: identical" in output.out
    assert "First divergence: none" in output.out
    assert "prompt_identical" in output.out
    assert "secret" not in output.out


def test_diff_reports_context_and_coverage_without_unknown_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _write_json(tmp_path / "first.json", future_key="private one")
    second = _write_json(tmp_path / "second.json", future_key="private two")
    assert main(["diff", str(first), str(second)]) == 0
    output = capsys.readouterr().out
    assert "Prompt status: identical" in output
    assert "Context differences: unmodeled_context" in output
    assert "Coverage warnings: unmodeled_context" in output
    assert "context_changed" in output
    assert "coverage_limited" in output
    assert "future_key" not in output
    assert "private" not in output


def test_analyze_one_request_is_insufficient(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "one.jsonl"
    path.write_text(
        '{"model":"demo","response_format":null,"messages":[]}', encoding="utf-8"
    )
    assert main(["analyze", str(path)]) == 0
    output = capsys.readouterr().out
    assert "Requests: 1" in output
    assert "Prompt status: insufficient_data" in output
    assert "insufficient_data" in output
    assert "Stable prefix estimate: unavailable" in output
    assert "Provider-reported cached usage: unavailable" in output


def test_diff_shows_advisory_known_model_minimum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _write_json(
        tmp_path / "first.json", model="claude-sonnet-4-6", system="private A"
    )
    second = _write_json(
        tmp_path / "second.json", model="claude-sonnet-4-6", system="private B"
    )
    assert main(["diff", str(first), str(second)]) == 0
    output = capsys.readouterr().out
    assert "Stable prefix estimate: ~" in output
    assert "(heuristic; wide;" in output
    assert "Documented model minimum: 1024 tokens (reviewed 2026-10-25)" in output
    assert "Estimate vs minimum: below (advisory only)" in output
    assert (
        "Minimum source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching"
        in output
    )
    assert "Provider-reported cached usage: unavailable" in output
    assert "private" not in output

    long_system = "a" * 4096
    _write_json(first, model="claude-sonnet-4-6", system=long_system + "X")
    _write_json(second, model="claude-sonnet-4-6", system=long_system + "Y")
    assert main(["diff", str(first), str(second)]) == 0
    assert "Estimate vs minimum: at or above (advisory only)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        ('{"model":"demo","messages":[]}', "ambiguous provider"),
        ('{"model":"demo","system":"secret","messages":[]}\n{', "invalid JSON"),
        ("  \n", "empty input"),
    ],
)
def test_analyze_invalid_input_has_no_partial_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: str,
    error: str,
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(contents, encoding="utf-8")
    assert main(["analyze", str(path)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert error in output.err
    assert "secret" not in output.err
    if error == "invalid JSON":
        assert f"{path}:2:" in output.err


def test_diff_rejects_incomparable_and_unreadable_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _write_json(tmp_path / "first.json")
    second = _write_json(tmp_path / "second.json", model="other")
    assert main(["diff", str(first), str(second)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "same provider and model" in output.err
    assert "other" not in output.err
    assert main(["diff", str(first), str(tmp_path / "missing.json")]) == 2
    assert "unreadable input file" in capsys.readouterr().err


def test_diff_rejects_invalid_json_and_provider_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _write_json(tmp_path / "first.json")
    second = tmp_path / "second.json"
    second.write_text("{", encoding="utf-8")
    assert main(["diff", str(first), str(second)]) == 2
    assert "invalid JSON" in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        main(["diff", str(first), str(second), "--provider", "other"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        (b" \n", "empty input"),
        (b"\xff", "invalid UTF-8"),
    ],
)
def test_diff_rejects_empty_or_non_utf8_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: bytes,
    error: str,
) -> None:
    first = _write_json(tmp_path / "first.json")
    second = tmp_path / "second.json"
    second.write_bytes(contents)
    assert main(["diff", str(first), str(second)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert error in output.err


def test_safe_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_json(tmp_path / "request.json")

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("private prompt")

    monkeypatch.setattr("cachelint.cli.read_json", fail)
    assert main(["diff", str(path), str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "cachelint: error: internal failure\n"
