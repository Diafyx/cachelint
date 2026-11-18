"""Offline, privacy-safe Anthropic breakpoint audit behavior."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import cachelint.breakpoints as breakpoint_module
from cachelint import (
    BreakpointAuditResult,
    BreakpointLimits,
    IncomparableRequestsError,
    InvalidRequestError,
    audit_breakpoints,
    canonicalize_request,
    render_breakpoint_json,
)
from cachelint.cli import main

MODEL = "claude-sonnet-4-6"
LONG = "stable private instructions " * 250


def _body(
    suffix: str,
    *,
    system_control: object | None = None,
    automatic: object | None = None,
    model: str = MODEL,
) -> dict[str, object]:
    system: dict[str, object] = {"type": "text", "text": LONG}
    if system_control is not None:
        system["cache_control"] = system_control
    body: dict[str, object] = {
        "model": model,
        "system": [system],
        "messages": [{"role": "user", "content": [{"type": "text", "text": suffix}]}],
    }
    if automatic is not None:
        body["cache_control"] = automatic
    return body


def _audit(left: dict[str, object], right: dict[str, object]) -> BreakpointAuditResult:
    return audit_breakpoints(
        [
            canonicalize_request(left, provider="anthropic"),
            canonicalize_request(right, provider="anthropic"),
        ]
    )


def _codes(result: BreakpointAuditResult) -> set[str]:
    return {finding.code for finding in result.findings}


def test_stable_boundary_without_controls_is_advisory_and_private() -> None:
    result = _audit(_body("PRIVATE-A"), _body("PRIVATE-B"))
    assert result.stable_boundary_path == "system[0]"
    assert result.prefix_estimate is not None
    assert result.prefix_estimate.minimum_comparison == "estimate_at_or_above"
    assert _codes(result) == {"plausible_missed_reuse"}
    output = render_breakpoint_json(result)
    assert "PRIVATE" not in output
    assert LONG not in output
    assert MODEL not in output
    assert "cachelint.breakpoints" in output
    assert output == render_breakpoint_json(result)


def test_explicit_control_at_stable_boundary_is_adequate() -> None:
    control = {"type": "ephemeral"}
    result = _audit(
        _body("first", system_control=control),
        _body("second", system_control=control),
    )
    assert result.control_mode == "explicit"
    assert _codes(result) == {"adequate_placement"}


def test_automatic_control_on_dynamic_suffix_is_candidate() -> None:
    control = {"type": "ephemeral"}
    result = _audit(
        _body("first", automatic=control),
        _body("second", automatic=control),
    )
    assert result.control_mode == "automatic"
    assert _codes(result) == {"plausible_missed_reuse"}


def test_automatic_target_with_identical_prompt_is_adequate() -> None:
    control = {"type": "ephemeral"}
    result = _audit(_body("same", automatic=control), _body("same", automatic=control))
    assert result.stable_boundary_path == "messages[0].content[0]"
    assert _codes(result) == {"adequate_placement"}


@pytest.mark.parametrize(
    ("control", "expected"),
    [
        ({"type": "other"}, "invalid_control"),
        ({"type": "ephemeral", "ttl": "2h"}, "invalid_control"),
    ],
)
def test_invalid_controls_are_reported_without_values(
    control: object, expected: str
) -> None:
    result = _audit(
        _body("a", system_control=control), _body("b", system_control=control)
    )
    assert expected in _codes(result)
    assert "2h" not in render_breakpoint_json(result)


def test_mixed_ttl_order_and_automatic_conflict() -> None:
    hour = {"type": "ephemeral", "ttl": "1h"}
    short = {"type": "ephemeral"}
    good = _body("same", system_control=hour)
    good["messages"] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "same", "cache_control": short}],
        }
    ]
    assert "ttl_order" not in _codes(_audit(good, good))
    bad = _body("same", system_control=short)
    bad["messages"] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "same", "cache_control": hour}],
        }
    ]
    assert "ttl_order" in _codes(_audit(bad, bad))
    conflict = _body("same", automatic=hour)
    conflict["messages"] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "same", "cache_control": short}],
        }
    ]
    assert "automatic_conflict" in _codes(_audit(conflict, conflict))


def test_four_explicit_plus_automatic_exceeds_limit() -> None:
    control = {"type": "ephemeral"}
    body = _body("same", automatic=control)
    body["system"] = [
        {"type": "text", "text": LONG, "cache_control": control} for _ in range(4)
    ]
    assert "too_many_breakpoints" in _codes(_audit(body, body))


def test_five_explicit_controls_exceed_limit_without_automatic() -> None:
    control = {"type": "ephemeral"}
    body = _body("same")
    body["system"] = [
        {"type": "text", "text": LONG, "cache_control": control} for _ in range(5)
    ]
    assert "too_many_breakpoints" in _codes(_audit(body, body))


def test_tool_boundary_and_unsupported_message_position() -> None:
    control = {"type": "ephemeral"}
    left = _body("first")
    right = _body("second")
    for body in (left, right):
        body.pop("system")
        body["tools"] = [
            {"name": "lookup", "description": LONG, "cache_control": control}
        ]
    result = _audit(left, right)
    assert result.stable_boundary_path == "tools[0]"
    assert "adequate_placement" in _codes(result)
    left["messages"] = [{"role": "user", "content": "first", "cache_control": control}]
    right["messages"] = [
        {"role": "user", "content": "second", "cache_control": control}
    ]
    assert "unsupported_position" in _codes(_audit(left, right))


def test_control_variance_or_changed_context_suppresses_recommendation() -> None:
    left = _body("first", system_control={"type": "ephemeral"})
    right = _body("second")
    result = _audit(left, right)
    assert result.control_mode == "varied"
    assert "coverage_limited" in _codes(result)
    assert "plausible_missed_reuse" not in _codes(result)
    left = _body("first")
    right = _body("second")
    left["tool_choice"] = {"type": "auto"}
    right["tool_choice"] = {"type": "any"}
    assert _codes(_audit(left, right)) == {"context_changed"}


def test_modeled_lookback_window_is_candidate_not_hit_prediction() -> None:
    control = {"type": "ephemeral"}
    left = _body("first", automatic=control)
    right = _body("second", automatic=control)
    for body, first in ((left, "first"), (right, "second")):
        body["messages"] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": first},
                    *({"type": "text", "text": "later"} for _ in range(20)),
                ],
            }
        ]
    assert _codes(_audit(left, right)) == {"lookback_window", "plausible_missed_reuse"}


def test_short_and_unknown_model_do_not_recommend_placement() -> None:
    short = _body("a")
    short["system"] = [{"type": "text", "text": "tiny"}]
    other = _body("b")
    other["system"] = [{"type": "text", "text": "tiny"}]
    assert _codes(_audit(short, other)) == {"short_stable_prefix"}
    assert _codes(
        _audit(_body("a", model="unknown-model"), _body("b", model="unknown-model"))
    ) == {"unknown_minimum"}


def test_length_threshold_uses_complete_boundary_not_later_partial_text() -> None:
    left = _body("same long partial " * 250 + "A")
    right = _body("same long partial " * 250 + "B")
    for body in (left, right):
        body["system"] = [{"type": "text", "text": "tiny"}]
    result = _audit(left, right)
    assert result.stable_boundary_path == "system[0]"
    assert result.prefix_estimate is not None
    assert result.prefix_estimate.minimum_comparison == "estimate_below"
    assert _codes(result) == {"short_stable_prefix"}


def test_stable_message_block_and_string_boundaries_are_estimated() -> None:
    left = _body("A")
    right = _body("B")
    for body in (left, right):
        body.pop("system")
    left["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": LONG},
                {"type": "text", "text": "A"},
            ],
        }
    ]
    right["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": LONG},
                {"type": "text", "text": "B"},
            ],
        }
    ]
    result = _audit(left, right)
    assert result.stable_boundary_path == "messages[0].content[0]"
    assert _codes(result) == {"plausible_missed_reuse"}
    for body in (left, right):
        body["messages"] = [{"role": "user", "content": LONG}]
    result = _audit(left, right)
    assert result.stable_boundary_path == "messages[0].content"
    assert _codes(result) == {"plausible_missed_reuse"}


def test_opaque_content_and_one_request_limit_placement() -> None:
    left = _body("a")
    right = _body("b")
    left["messages"] = [
        {"role": "user", "content": [{"type": "image", "source": "PRIVATE"}]}
    ]
    right["messages"] = [
        {"role": "user", "content": [{"type": "image", "source": "PRIVATE"}]}
    ]
    result = _audit(left, right)
    assert "coverage_limited" in _codes(result)
    assert "PRIVATE" not in render_breakpoint_json(result)
    single = audit_breakpoints([canonicalize_request(_body("a"), provider="anthropic")])
    assert _codes(single) == {"insufficient_data"}


def test_limits_reject_excess_work() -> None:
    request = canonicalize_request(_body("a"), provider="anthropic")
    with pytest.raises(InvalidRequestError, match="block limit"):
        audit_breakpoints([request, request], limits=BreakpointLimits(max_blocks=3))
    with pytest.raises(InvalidRequestError, match="positive integers"):
        BreakpointLimits(max_blocks=0)
    with pytest.raises(InvalidRequestError, match="expected breakpoint limits"):
        audit_breakpoints([request], limits=cast(BreakpointLimits, object()))
    with pytest.raises(InvalidRequestError, match="cohort empty"):
        audit_breakpoints([])
    with pytest.raises(InvalidRequestError, match="request limit"):
        audit_breakpoints([request, request], limits=BreakpointLimits(max_requests=1))
    with pytest.raises(InvalidRequestError, match="expected canonical"):
        audit_breakpoints([cast(object, request), cast(object, "PRIVATE")])  # type: ignore[list-item]
    with pytest.raises(IncomparableRequestsError, match="Anthropic"):
        audit_breakpoints(
            [
                request,
                canonicalize_request(_body("a", model="other"), provider="anthropic"),
            ]
        )


def test_invalid_position_and_automatic_target_cases() -> None:
    control = {"type": "ephemeral"}
    empty = _body("a", system_control=control)
    empty["system"] = [{"type": "text", "text": "", "cache_control": control}]
    assert "unsupported_position" in _codes(_audit(empty, empty))
    bad_auto = _body("a", automatic={"type": "other"})
    assert "invalid_control" in _codes(_audit(bad_auto, bad_auto))
    no_target: dict[str, object] = {
        "model": MODEL,
        "messages": [],
        "cache_control": control,
    }
    assert "automatic_no_target" in _codes(_audit(no_target, no_target))
    same = _body("same", automatic=control)
    same["messages"] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "same", "cache_control": control}],
        }
    ]
    assert "automatic_conflict" not in _codes(_audit(same, same))
    assert "too_many_breakpoints" not in _codes(_audit(same, same))


def test_no_stable_cacheable_boundary_and_renderer_validation() -> None:
    left: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "A"}],
    }
    right: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "B"}],
    }
    result = _audit(left, right)
    assert _codes(result) == {"no_stable_boundary"}
    with pytest.raises(InvalidRequestError, match="expected breakpoint audit result"):
        render_breakpoint_json(cast(BreakpointAuditResult, object()))
    bad_finding = replace(result.findings[0], action="PRIVATE")
    with pytest.raises(InvalidRequestError, match="unsupported breakpoint wording"):
        render_breakpoint_json(replace(result, findings=(bad_finding,)))


def test_corrupt_bundled_rule_snapshot_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "breakpoint_rules_v1.json").write_text(
        '{"schema_version": 2}', encoding="utf-8"
    )
    monkeypatch.setattr(breakpoint_module, "files", lambda package: tmp_path)
    with pytest.raises(ValueError, match="invalid breakpoint rule snapshot"):
        breakpoint_module._load_rules()


def test_cli_json_is_private_and_output_is_atomic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "private.jsonl"
    source.write_text(
        "\n".join(json.dumps(_body(value)) for value in ("PRIVATE-A", "PRIVATE-B"))
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    assert (
        main(["breakpoints", str(source), "--format", "json", "--output", str(output)])
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["report_type"] == "cachelint.breakpoints"
    assert "PRIVATE" not in output.read_text(encoding="utf-8")
    assert main(["breakpoints", str(source), "--output", str(source)]) == 2
    assert "PRIVATE" not in capsys.readouterr().err


def test_cli_text_and_unreadable_or_oversized_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "private.jsonl"
    source.write_text(
        "\n".join(json.dumps(_body(value)) for value in ("PRIVATE-A", "PRIVATE-B"))
        + "\n",
        encoding="utf-8",
    )
    assert main(["breakpoints", str(source)]) == 0
    assert "plausible_missed_reuse" in capsys.readouterr().out
    assert main(["breakpoints", str(tmp_path / "missing.jsonl")]) == 2
    assert "unreadable input file" in capsys.readouterr().err
    oversized = tmp_path / "oversized.jsonl"
    with oversized.open("wb") as stream:
        stream.seek(16 * 1024 * 1024)
        stream.write(b"x")
    assert main(["breakpoints", str(oversized)]) == 2
    assert "oversized input file" in capsys.readouterr().err
