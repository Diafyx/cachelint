"""Offline usage joins preserve unknowns and reject ambiguous private traces."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import cast

import httpx2
import pytest

from cachelint import InvalidRequestError, UsageLimits, UsageTrace, analyze_usage
from cachelint.cli import main
from cachelint.recording import RequestRecorder
from cachelint.report import render_usage_json, render_usage_text, usage_report

SECRET = "__CACHELINT_PRIVATE_PROMPT_AND_CREDENTIAL__"


def body(provider: str, model: str = "private-model") -> dict[str, object]:
    result: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": SECRET}],
    }
    if provider == "anthropic":
        result["system"] = "private system"
    else:
        result["prompt_cache_key"] = "private cache key"
    return result


def write_trace(
    root: Path,
    name: str,
    provider: str,
    rows: list[dict[str, object]],
    *,
    models: tuple[str, ...] | None = None,
) -> UsageTrace:
    requests = root / f"{name}.jsonl"
    sidecar = root / f"{name}.jsonl.usage.jsonl"
    bodies = [
        body(provider, model) for model in (models or ("private-model",) * len(rows))
    ]
    requests.write_bytes(
        b"".join((json.dumps(value) + "\n").encode() for value in bodies)
    )
    sidecar.write_bytes(b"".join((json.dumps(value) + "\n").encode() for value in rows))
    return UsageTrace(requests, sidecar)


def row(
    attempt: int,
    offset: int | None,
    *,
    status: str = "reported",
    usage: dict[str, int] | None = None,
    capture: str = "written",
    code: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "attempt_id": attempt,
        "capture_status": capture,
        "usage_status": status,
    }
    if offset is not None:
        result["request_offset"] = offset
    if usage is not None:
        result["usage"] = usage
    if code is not None:
        result["capture_code"] = code
    return result


def offsets(trace: UsageTrace) -> list[int]:
    data = Path(trace.requests).read_bytes()
    lines = data.splitlines(keepends=True)
    return [sum(map(len, lines[:index])) for index in range(len(lines))]


def test_anthropic_join_retries_and_missing_evidence(tmp_path: Path) -> None:
    trace = write_trace(
        tmp_path,
        "trace",
        "anthropic",
        [],
        models=("secret-model-a", "secret-model-a", "secret-model-b"),
    )
    first, second, third = offsets(trace)
    Path(trace.sidecar).write_text(
        "".join(
            json.dumps(value) + "\n"
            for value in (
                row(2, second, usage={"cache_read_input_tokens": 0}),
                row(
                    1,
                    first,
                    usage={
                        "cache_read_input_tokens": 8,
                        "cache_creation_input_tokens": 3,
                    },
                ),
                row(
                    4,
                    None,
                    capture="skipped",
                    code="credential_field",
                    status="unmatched",
                ),
            )
        ),
        encoding="utf-8",
    )
    result = analyze_usage(trace, provider="anthropic")
    assert result.request_count == 3
    assert result.sidecar_row_count == 3
    assert result.unassigned_attempt_count == 1
    assert [(item.attempt_id, item.usage_status) for item in result.observations] == [
        (1, "reported"),
        (2, "reported"),
        (None, "missing_sidecar"),
    ]
    assert result.groups[0].request_count == 2
    assert result.groups[0].cache_read_positive_count == 1
    assert result.groups[0].cache_read_zero_count == 1
    assert result.groups[0].known_cache_read_tokens == 8
    assert result.groups[0].cache_write_positive_count == 1
    assert result.groups[0].cache_write_unknown_count == 1
    assert result.groups[1].cache_read_unknown_count == 1
    assert result.groups[1].status_counts[-1] == ("missing_sidecar", 1)
    assert SECRET not in repr(result)
    assert "secret-model" not in repr(result)
    assert SECRET not in render_usage_json(result)
    assert "private system" not in render_usage_text(result)
    assert "private-model" not in render_usage_json(result)
    assert third > second


def test_actual_recorder_sidecar_joins_without_provider_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class HookClient:
        def __init__(self, *, event_hooks: dict[str, list[object]]) -> None:
            self.request_hook = event_hooks["request"][0]
            self.response_hook = event_hooks["response"][0]

    sdk = ModuleType("fake_join_sdk")
    sdk.DefaultHttpxClient = HookClient  # type: ignore[attr-defined]
    monkeypatch.setattr(RequestRecorder, "_load_sdk", lambda self: sdk)
    path = tmp_path / "requests.jsonl"
    with RequestRecorder(
        path,
        provider="anthropic",
        capture_usage=True,
        allow_windows_acl_inheritance=True,
    ) as recorder:
        hooks = recorder.http_client()
        request = httpx2.Request(
            "POST", "https://api.anthropic.com/v1/messages", json=body("anthropic")
        )
        hooks.request_hook(request)
        hooks.response_hook(
            httpx2.Response(
                200, request=request, json={"usage": {"cache_read_input_tokens": 7}}
            )
        )
    result = analyze_usage(
        UsageTrace(path, Path(str(path) + ".usage.jsonl")), provider="anthropic"
    )
    assert result.observations[0].attempt_id == 1
    assert result.observations[0].cache_read_tokens == 7
    assert result.groups[0].known_cache_read_tokens == 7


def test_openai_multi_trace_sorting_and_unknown_write(tmp_path: Path) -> None:
    later = write_trace(
        tmp_path, "z", "openai", [row(1, 0, usage={"cached_tokens": 4})]
    )
    earlier = write_trace(
        tmp_path, "a", "openai", [row(1, 0, usage={"prompt_tokens": 12})]
    )
    result = analyze_usage([later, earlier], provider="openai")
    assert result == analyze_usage([earlier, later], provider="openai")
    assert result.trace_count == 2
    assert [
        (item.trace_index, item.attempt_id, item.cache_read_tokens)
        for item in result.observations
    ] == [(1, 1, None), (2, 1, 4)]
    group = result.groups[0]
    assert group.cache_read_positive_count == 1
    assert group.cache_read_unknown_count == 1
    assert group.cache_write_unknown_count == 2
    assert group.known_cache_write_tokens == 0
    assert dict(group.status_counts)["reported"] == 2
    document = usage_report(result)
    assert document["schema_version"] == 1
    assert document["report_type"] == "cachelint.usage"
    summary = cast(dict[str, object], document["summary"])
    groups = cast(list[dict[str, object]], document["groups"])
    counts = cast(dict[str, int], groups[0]["status_counts"])
    assert summary["byte_count"] == result.byte_count
    assert counts["reported"] == 2
    assert json.loads(render_usage_json(result)) == document
    assert render_usage_json(result).endswith("\n")


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"schema_version": 2}, "schema version"),
        ({"schema_version": True}, "schema version"),
        ({"attempt_id": 0}, "attempt ID"),
        ({"attempt_id": True}, "attempt ID"),
        ({"capture_status": "alien"}, "status"),
        ({"capture_status": []}, "status"),
        ({"usage_status": []}, "status"),
        ({"request_offset": -1}, "written usage coordinate"),
        ({"request_offset": True}, "written usage coordinate"),
        ({"capture_code": "size_limit"}, "written usage coordinate"),
        ({"usage": {}}, "usage counters"),
        ({"usage": {"cached_tokens": 1}}, "usage counters"),
        ({"usage": {"cache_read_input_tokens": True}}, "usage counters"),
        ({"usage": {"cache_read_input_tokens": -1}}, "usage counters"),
        ({"usage": {"cache_read_input_tokens": 1.5}}, "usage counters"),
        ({"usage": {"cache_read_input_tokens": 1}, "private": 2}, "row"),
    ],
)
def test_invalid_sidecar_rows_are_rejected(
    tmp_path: Path, bad: dict[str, object], reason: str
) -> None:
    trace = write_trace(
        tmp_path,
        "trace",
        "anthropic",
        [row(1, 0, usage={"cache_read_input_tokens": 1})],
    )
    value = row(1, 0, usage={"cache_read_input_tokens": 1}) | bad
    Path(trace.sidecar).write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(InvalidRequestError, match=reason) as error:
        analyze_usage(trace, provider="anthropic")
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (
            row(1, None, capture="skipped", code="other", status="absent"),
            "capture code",
        ),
        (row(1, None, capture="skipped", status="absent"), "capture code"),
        (
            row(1, None, capture="failed", code="size_limit", status="absent"),
            "capture code",
        ),
        (
            row(1, None, capture="unmatched", code="write_failed", status="unmatched"),
            "capture code",
        ),
        (
            row(1, 0, capture="skipped", code="size_limit", status="absent"),
            "request offset",
        ),
        (row(1, None, status="absent", capture="written"), "written usage coordinate"),
        (
            row(1, 0, status="absent", usage={"cache_read_input_tokens": 0}),
            "unexpected usage counters",
        ),
    ],
)
def test_sidecar_status_coordinate_invariants(
    tmp_path: Path, value: dict[str, object], reason: str
) -> None:
    trace = write_trace(tmp_path, "trace", "anthropic", [value])
    with pytest.raises(InvalidRequestError, match=reason):
        analyze_usage(trace, provider="anthropic")


def test_duplicate_ids_offsets_and_missing_targets_fail_safely(tmp_path: Path) -> None:
    trace = write_trace(tmp_path, "trace", "openai", [row(1, 0, status="absent")])
    sidecar = Path(trace.sidecar)
    for values, reason in (
        (
            [
                row(1, 0, status="absent"),
                row(1, None, capture="unmatched", status="unmatched"),
            ],
            "duplicate usage attempt ID",
        ),
        (
            [row(1, 0, status="absent"), row(2, 0, status="absent")],
            "duplicate usage request offset",
        ),
        ([row(1, 5, status="absent")], "offset has no complete request"),
    ):
        sidecar.write_text(
            "".join(json.dumps(value) + "\n" for value in values), encoding="utf-8"
        )
        with pytest.raises(InvalidRequestError, match=reason):
            analyze_usage(trace, provider="openai")


def test_partial_jsonl_duplicate_keys_and_invalid_utf8(tmp_path: Path) -> None:
    trace = write_trace(tmp_path, "trace", "anthropic", [row(1, 0, status="absent")])
    sidecar = Path(trace.sidecar)
    requests = Path(trace.requests)
    good_request = requests.read_bytes()
    good_sidecar = sidecar.read_bytes()
    sidecar.write_bytes(good_sidecar.rstrip(b"\n"))
    with pytest.raises(InvalidRequestError, match="incomplete JSONL line"):
        analyze_usage(trace, provider="anthropic")
    sidecar.write_bytes(good_sidecar)
    requests.write_bytes(good_request.rstrip(b"\n"))
    with pytest.raises(InvalidRequestError, match="incomplete JSONL line"):
        analyze_usage(trace, provider="anthropic")
    requests.write_bytes(good_request)
    sidecar.write_bytes(b'{"schema_version":1,"schema_version":1}\n')
    with pytest.raises(InvalidRequestError, match="invalid usage sidecar JSON"):
        analyze_usage(trace, provider="anthropic")
    sidecar.write_bytes(b"\xff\n")
    with pytest.raises(InvalidRequestError, match="invalid usage sidecar JSON"):
        analyze_usage(trace, provider="anthropic")
    requests.write_bytes(b"\xff\n")
    sidecar.write_bytes(good_sidecar)
    with pytest.raises(InvalidRequestError, match="invalid UTF-8"):
        analyze_usage(trace, provider="anthropic")


def test_cli_usage_json_text_and_safe_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = write_trace(
        tmp_path,
        "trace",
        "openai",
        [row(1, 0, usage={"cached_tokens": 3})],
    )
    arguments = [
        "usage",
        "--provider",
        "openai",
        "--trace",
        str(trace.requests),
        str(trace.sidecar),
    ]
    assert main(arguments) == 0
    text = capsys.readouterr().out
    assert "Cache read: positive 1" in text
    assert SECRET not in text
    output = tmp_path / "report.json"
    assert main([*arguments, "--format", "json", "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["summary"]["provider"] == "openai"
    assert document["observations"][0]["cache_read_tokens"] == 3
    assert main([*arguments, "--output", str(trace.requests)]) == 2
    assert "must not have a JSONL extension" in capsys.readouterr().err


def test_empty_pair_reports_no_cohorts(tmp_path: Path) -> None:
    trace = write_trace(tmp_path, "empty", "openai", [])
    result = analyze_usage(trace, provider="openai")
    assert result.groups == ()
    assert result.observations == ()
    assert "No request cohorts found." in render_usage_text(result)


def test_valid_failed_attempt_and_blank_request_line(tmp_path: Path) -> None:
    trace = write_trace(tmp_path, "trace", "anthropic", [])
    requests = Path(trace.requests)
    requests.write_bytes(b"\n" + (json.dumps(body("anthropic")) + "\n").encode())
    Path(trace.sidecar).write_text(
        json.dumps(
            row(2, None, capture="failed", code="write_failed", status="unmatched")
        )
        + "\n"
        + json.dumps(row(1, 1, usage={"cache_read_input_tokens": 0}))
        + "\n",
        encoding="utf-8",
    )
    result = analyze_usage(trace, provider="anthropic")
    assert result.request_count == 1
    assert result.unassigned_attempt_count == 1
    assert result.observations[0].cache_read_tokens == 0
    assert result.groups[0].cache_read_zero_count == 1


def test_input_identity_and_file_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = write_trace(tmp_path, "trace", "openai", [])
    with pytest.raises(InvalidRequestError, match="at least one usage trace"):
        analyze_usage([], provider="openai")
    with pytest.raises(InvalidRequestError, match="UsageTrace values"):
        analyze_usage([object()], provider="openai")  # type: ignore[list-item]
    with pytest.raises(InvalidRequestError, match="duplicate usage input file"):
        analyze_usage([trace, trace], provider="openai")
    for invalid in ("", 123):
        with pytest.raises(InvalidRequestError, match="nonempty strings or Paths"):
            analyze_usage(UsageTrace(invalid, trace.sidecar), provider="openai")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="unreadable usage input"):
        analyze_usage(
            UsageTrace(tmp_path / "missing.jsonl", trace.sidecar), provider="openai"
        )
    with pytest.raises(InvalidRequestError, match="regular file"):
        analyze_usage(UsageTrace(tmp_path, trace.sidecar), provider="openai")

    original_open = Path.open

    def fail_open(self: Path, mode: str = "r") -> object:
        if self == Path(trace.sidecar).resolve():
            raise OSError("private operating-system detail")
        assert mode == "rb"
        return original_open(self, "rb")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_open)
        with pytest.raises(
            InvalidRequestError, match="unreadable usage input"
        ) as error:
            analyze_usage(trace, provider="openai")
    assert "private operating-system detail" not in str(error.value)


def test_symlink_trace_is_rejected(tmp_path: Path) -> None:
    trace = write_trace(tmp_path, "trace", "anthropic", [])
    link = tmp_path / "linked.jsonl"
    try:
        link.symlink_to(Path(trace.requests))
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(InvalidRequestError, match="symlink input"):
        analyze_usage(UsageTrace(link, trace.sidecar), provider="anthropic")


def test_limits_and_invalid_configuration(tmp_path: Path) -> None:
    trace = write_trace(
        tmp_path,
        "trace",
        "anthropic",
        [
            row(1, 0, status="absent"),
            row(2, None, capture="unmatched", status="unmatched"),
        ],
        models=("model-a", "model-b"),
    )
    second = write_trace(tmp_path, "second", "anthropic", [])
    for limits, reason in (
        (UsageLimits(max_traces=1), "trace limit"),
        (UsageLimits(max_bytes=1), "byte limit"),
        (UsageLimits(max_requests=1), "request limit"),
        (UsageLimits(max_rows=1), "row limit"),
    ):
        with pytest.raises(InvalidRequestError, match=reason):
            analyze_usage(
                [trace, second] if reason == "trace limit" else trace,
                provider="anthropic",
                limits=limits,
            )
    with pytest.raises(InvalidRequestError, match="positive integers"):
        UsageLimits(max_rows=True)
    with pytest.raises(InvalidRequestError, match="invalid usage limits"):
        analyze_usage(trace, provider="anthropic", limits=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="unsupported usage provider"):
        analyze_usage(trace, provider="other")  # type: ignore[arg-type]
