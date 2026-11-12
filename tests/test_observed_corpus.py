"""Opt-in corpus findings join observations without exposing private inputs."""

from __future__ import annotations

import json
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pytest

from cachelint import (
    CorpusUsageTrace,
    InvalidRequestError,
    Provider,
    UsageLimits,
    analyze_corpus,
    analyze_observed_corpus,
)
from cachelint.cli import main
from cachelint.report import corpus_report, render_corpus_json, render_corpus_text

SECRET = "PRIVATE_CANARY_CREDENTIAL_PRIVATE_CANARY_RESPONSE_ID_PRIVATE_CANARY_HASH"


def _body(provider: str, text: str, *, context: bool = False) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "claude-sonnet-4-6" if provider == "anthropic" else "gpt-5.6-sol",
        "messages": [{"role": "user", "content": text}],
    }
    if provider == "anthropic":
        body["system"] = SECRET
        if context:
            body["temperature"] = 0.5
    else:
        body["prompt_cache_key"] = SECRET
        if context:
            body["temperature"] = 0.5
    return body


def _trace(
    root: Path,
    provider: str,
    bodies: list[dict[str, object]],
    counters: list[int | None],
) -> CorpusUsageTrace:
    request_path = root / f"{provider}.jsonl"
    sidecar_path = root / f"{provider}.jsonl.usage.jsonl"
    offsets: list[int] = []
    data = b""
    for body in bodies:
        offsets.append(len(data))
        data += (json.dumps(body) + "\n").encode("utf-8")
    request_path.write_bytes(data)
    rows = []
    for index, (offset, count) in enumerate(zip(offsets, counters, strict=True), 1):
        if count is None:
            continue
        key = "cache_read_input_tokens" if provider == "anthropic" else "cached_tokens"
        rows.append(
            {
                "schema_version": 1,
                "attempt_id": index,
                "capture_status": "written",
                "request_offset": offset,
                "usage_status": "reported",
                "usage": {key: count},
            }
        )
    sidecar_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return CorpusUsageTrace(cast(Provider, provider), request_path, sidecar_path)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_observed_findings_and_private_reports(tmp_path: Path, provider: str) -> None:
    first = _body(provider, SECRET + "A")
    trace = _trace(
        tmp_path,
        provider,
        [first, first, _body(provider, SECRET + "B"), first],
        [0, 0, 0, 0],
    )
    result = analyze_observed_corpus(trace.requests, usage_traces=[trace])
    assert result.trace_count == 1
    assert result.traced_request_count == 4
    assert result.usage_groups[0].cache_read_zero_count == 4
    codes = [finding.code for finding in result.findings]
    assert "matching_prefix_no_read" in codes
    assert "divergent_prefix_no_read" in codes
    assert "short_modeled_prefix" in codes
    assert all(
        finding.rank == index for index, finding in enumerate(result.findings, 1)
    )
    document = corpus_report(result)
    assert document["schema_version"] == 2
    summary = cast(dict[str, object], document["summary"])
    assert summary["provider_reported_cache_status"] == "sidecar"
    groups = cast(list[dict[str, object]], document["groups"])
    usage = cast(dict[str, object], groups[0]["usage"])
    statuses = cast(dict[str, int], usage["status_counts"])
    assert statuses["reported"] == 4
    assert corpus_report(analyze_corpus(trace.requests))["schema_version"] == 1
    for output in (
        repr(result),
        render_corpus_json(result),
        render_corpus_text(result),
    ):
        assert SECRET not in output
        assert "PRIVATE_CANARY_CREDENTIAL" not in output
        assert "PRIVATE_CANARY_RESPONSE_ID" not in output
        assert "PRIVATE_CANARY_HASH" not in output
        assert str(tmp_path) not in output
        assert "claude-sonnet-4-6" not in output
        assert "gpt-5.6-sol" not in output
    assert render_corpus_json(result) == render_corpus_json(
        analyze_observed_corpus(trace.requests, usage_traces=[trace])
    )


def test_missing_context_coverage_and_directory_discovery(tmp_path: Path) -> None:
    first = _body("anthropic", SECRET)
    opaque = _body("anthropic", SECRET + "changed", context=True)
    opaque["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": SECRET,
                    },
                }
            ],
        }
    ]
    trace = _trace(tmp_path, "anthropic", [first, opaque, first], [0, 0, None])
    result = analyze_observed_corpus(tmp_path, usage_traces=[trace])
    assert result.corpus.file_count == 1
    assert result.usage_groups[0].status_counts[-2:] == (
        ("missing_sidecar", 1),
        ("missing_trace", 0),
    )
    codes = {finding.code for finding in result.findings}
    assert {
        "context_changed_no_read",
        "coverage_limited",
        "missing_observation",
    } <= codes
    assert "read_with_divergence" not in codes


def test_positive_read_with_divergence_and_untraced_file(tmp_path: Path) -> None:
    first = _body("openai", SECRET + "A")
    trace = _trace(
        tmp_path,
        "openai",
        [first, _body("openai", SECRET + "B"), first],
        [0, 7, 7],
    )
    extra = tmp_path / "extra.jsonl"
    extra.write_text(json.dumps(first) + "\n", encoding="utf-8")
    result = analyze_observed_corpus([trace.requests, extra], usage_traces=[trace])
    assert any(f.code == "read_with_divergence" for f in result.findings)
    assert result.usage_groups[0].status_counts[-1] == ("missing_trace", 1)


def test_positive_read_below_minimum_and_unknown_model(tmp_path: Path) -> None:
    first = _body("openai", SECRET + "A")
    trace = _trace(tmp_path, "openai", [first, _body("openai", SECRET + "B")], [0, 7])
    result = analyze_observed_corpus(trace.requests, usage_traces=[trace])
    assert {"read_with_divergence", "read_below_modeled_minimum"} <= {
        finding.code for finding in result.findings
    }
    first["model"] = "PRIVATE_MODEL"
    unknown = _trace(tmp_path, "openai", [first, first], [0, 0])
    result = analyze_observed_corpus(unknown.requests, usage_traces=[unknown])
    assert "short_modeled_prefix" not in {finding.code for finding in result.findings}


def test_structural_and_long_prefix_findings(tmp_path: Path) -> None:
    long = SECRET * 200
    first = _body("anthropic", long)
    first["system"] = long
    changed = _body("anthropic", long)
    changed["system"] = long
    changed["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": long}]}
    ]
    trace = _trace(tmp_path, "anthropic", [first, changed, first], [0, 0, 0])
    result = analyze_observed_corpus(trace.requests, usage_traces=[trace])
    assert result.corpus.patterns[0].text_offset_min is None
    assert "short_modeled_prefix" not in {finding.code for finding in result.findings}
    assert any(f.code == "divergent_prefix_no_read" for f in result.findings)


@pytest.mark.parametrize("change", ["path", "content", "count"])
def test_trace_change_between_reads_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from cachelint import _evidence
    from cachelint._observed import _analyze_usage_with_requests

    trace = _trace(tmp_path, "anthropic", [_body("anthropic", SECRET)] * 2, [0, 0])
    original = _analyze_usage_with_requests

    def changed(*args: Any, **kwargs: Any) -> object:
        usage, requests = original(*args, **kwargs)
        if change == "path":
            return usage, ((tmp_path / "other.jsonl", requests[0][1]), *requests[1:])
        if change == "content":
            return usage, (
                (requests[0][0], replace(requests[0][1], model="changed")),
                *requests[1:],
            )
        return replace(usage, observations=usage.observations[:1]), requests[:1]

    monkeypatch.setattr(_evidence, "_analyze_usage_with_requests", changed)
    with pytest.raises(InvalidRequestError, match="changed during corpus analysis"):
        analyze_observed_corpus(trace.requests, usage_traces=[trace])


def test_mixed_provider_traces_share_limits(tmp_path: Path) -> None:
    anthropic = _trace(tmp_path, "anthropic", [_body("anthropic", SECRET)], [0])
    openai = _trace(tmp_path, "openai", [_body("openai", SECRET)], [1])
    both = [anthropic, openai]
    result = analyze_observed_corpus(tmp_path, usage_traces=both)
    assert result.trace_count == 2
    assert result.traced_request_count == 2
    assert [group.provider for group in result.usage_groups] == ["anthropic", "openai"]
    with pytest.raises(InvalidRequestError, match="invalid usage limits"):
        analyze_observed_corpus(
            tmp_path, usage_traces=both, usage_limits=cast(UsageLimits, object())
        )
    with pytest.raises(InvalidRequestError, match="usage trace limit"):
        analyze_observed_corpus(
            tmp_path, usage_traces=both, usage_limits=UsageLimits(max_traces=1)
        )
    with pytest.raises(InvalidRequestError, match="combined corpus usage limit"):
        analyze_observed_corpus(
            tmp_path, usage_traces=both, usage_limits=UsageLimits(max_requests=1)
        )
    with pytest.raises(InvalidRequestError, match="usage byte limit"):
        analyze_observed_corpus(
            tmp_path,
            usage_traces=both,
            usage_limits=UsageLimits(
                max_bytes=(
                    Path(anthropic.requests).stat().st_size
                    + Path(anthropic.sidecar).stat().st_size
                    + 1
                )
            ),
        )


def test_trace_validation_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _trace(tmp_path, "anthropic", [_body("anthropic", SECRET)] * 2, [0, 0])
    with pytest.raises(InvalidRequestError, match="at least one"):
        analyze_observed_corpus(trace.requests, usage_traces=[])
    with pytest.raises(InvalidRequestError, match="duplicate corpus usage"):
        analyze_observed_corpus(trace.requests, usage_traces=[trace, trace])
    with pytest.raises(InvalidRequestError, match="CorpusUsageTrace"):
        analyze_observed_corpus(
            trace.requests, usage_traces=[cast(CorpusUsageTrace, object())]
        )
    outside = tmp_path / "other.jsonl"
    outside.write_text(json.dumps(_body("anthropic", SECRET)) + "\n", encoding="utf-8")
    with pytest.raises(InvalidRequestError, match="outside the corpus"):
        analyze_observed_corpus(outside, usage_traces=[trace])
    with pytest.raises(InvalidRequestError, match="usage sidecar"):
        analyze_corpus(trace.sidecar)
    assert (
        main(
            [
                "corpus",
                str(tmp_path),
                "--format",
                "json",
                "--usage-trace",
                "anthropic",
                str(trace.requests),
                str(trace.sidecar),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert json.loads(output)["schema_version"] == 2
    assert SECRET not in output
    assert (
        main(
            [
                "corpus",
                str(tmp_path),
                "--usage-trace",
                "other",
                str(trace.requests),
                str(trace.sidecar),
            ]
        )
        == 2
    )
    assert "unsupported corpus usage provider" in capsys.readouterr().err
    assert files("cachelint").joinpath("corpus_report_v2.schema.json").is_file()
