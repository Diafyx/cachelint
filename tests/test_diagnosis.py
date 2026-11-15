"""Synthetic positive and negative controls for content-safe diagnosis."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cachelint import (
    CanonicalRequest,
    DiagnosisLimits,
    IncomparableRequestsError,
    InvalidRequestError,
    canonicalize_request,
    diagnose_requests,
)

_CANARY = "PRIVATE_DIAGNOSIS_CANARY"


def _request(provider: str, first: str, *, tail: int = 0) -> CanonicalRequest:
    messages: list[dict[str, object]] = [
        {"role": "user", "content": first if provider == "openai" else _CANARY}
    ]
    messages.extend(
        {"role": "assistant", "content": f"fixed {index}"} for index in range(tail)
    )
    body: dict[str, object] = {"model": "demo", "messages": messages}
    if provider == "anthropic":
        body["system"] = first
    else:
        body["response_format"] = {"type": "text"}
    return canonicalize_request(body)


def _codes(*requests: CanonicalRequest) -> list[str]:
    return [finding.code for finding in diagnose_requests(requests).findings]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_valid_timestamp_is_candidate_but_invalid_date_is_not(provider: str) -> None:
    before = _request(provider, "Started 2026-11-14T09:12:13Z")
    after = _request(provider, "Started 2026-11-15T09:12:13Z")
    result = diagnose_requests([before, after, after])
    assert [(item.code, item.count, item.evidence) for item in result.findings] == [
        ("volatile_timestamp", 2, "candidate")
    ]
    assert result.provider == provider
    assert result.request_count == 3
    assert result.paired_count == 2
    assert _CANARY not in repr(result)
    assert "2026-" not in repr(result)

    invalid = diagnose_requests(
        [
            _request(provider, "Started 2026-13-99T09:12:13Z"),
            _request(provider, "Started 2026-14-99T09:12:13Z"),
        ]
    )
    assert [item.code for item in invalid.findings] == ["unclassified_drift"]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_identifier_requires_complete_uuid_shape(provider: str) -> None:
    before = _request(provider, "Run 123e4567-e89b-12d3-a456-426614174000")
    after = _request(provider, "Run 123e4567-e89b-12d3-a456-426614174001")
    result = diagnose_requests([before, after])
    assert [item.code for item in result.findings] == ["volatile_identifier"]
    assert result.findings[0].evidence == "candidate"
    assert "123e4567" not in repr(result)

    lookalike = diagnose_requests(
        [_request(provider, "Run abc-123"), _request(provider, "Run abc-124")]
    )
    assert [item.code for item in lookalike.findings] == ["unclassified_drift"]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_tool_order_is_observed_only_for_same_definitions(provider: str) -> None:
    marker: dict[str, object] = (
        {"system": "Rules"} if provider == "anthropic" else {"response_format": {}}
    )

    def request(tools: list[dict[str, object]]) -> CanonicalRequest:
        return canonicalize_request(
            {
                "model": "demo",
                "messages": [{"role": "user", "content": "fixed"}],
                "tools": tools,
                **marker,
            }
        )

    first = request([{"name": "private-A"}, {"name": "private-B"}])
    swapped = request([{"name": "private-B"}, {"name": "private-A"}])
    changed = request([{"name": "private-C"}, {"name": "private-B"}])
    found = diagnose_requests([first, swapped]).findings
    assert [item.code for item in found] == ["tool_order_churn"]
    assert found[0].path == "tools[0]"
    assert found[0].evidence == "observed"
    assert "private-A" not in repr(found)
    assert "tool_order_churn" not in _codes(first, changed)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_whitespace_drift_does_not_hide_other_text_changes(provider: str) -> None:
    before = _request(provider, "Stay  calm\nand clear")
    spacing = _request(provider, "Stay calm and   clear")
    changed = _request(provider, "Stay calm and sharp")
    assert _codes(before, spacing) == ["whitespace_drift"]
    assert _codes(before, changed) == ["unclassified_drift"]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_repeated_later_segments_are_a_candidate(provider: str) -> None:
    before = _request(provider, "Changing A", tail=2)
    after = _request(provider, "Changing B", tail=2)
    found = diagnose_requests([before, after]).findings
    assert [item.code for item in found] == ["stable_suffix_after_drift"]
    assert found[0].evidence == "candidate"
    assert "stable_suffix_after_drift" not in _codes(
        _request(provider, "Changing A"), _request(provider, "Changing B")
    )


def test_structural_change_before_stable_suffix_is_not_movement_advice() -> None:
    first = canonicalize_request(
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {"role": "user", "content": "fixed"},
                {"role": "assistant", "content": "later one"},
                {"role": "user", "content": "later two"},
            ],
        }
    )
    changed = canonicalize_request(
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {"role": "assistant", "content": "fixed"},
                {"role": "assistant", "content": "later one"},
                {"role": "user", "content": "later two"},
            ],
        }
    )
    assert _codes(first, changed) == ["unclassified_drift"]


def test_added_later_segment_does_not_claim_a_stable_suffix() -> None:
    first = _request("anthropic", "Changing A", tail=2)
    changed = _request("anthropic", "Changing B", tail=3)
    assert _codes(first, changed) == ["unclassified_drift"]


def test_supported_text_blocks_get_safe_paths() -> None:
    anthropic_a = canonicalize_request(
        {
            "model": "demo",
            "system": [{"type": "text", "text": "At 2026-11-14T09:12:13Z"}],
            "messages": [],
        }
    )
    anthropic_b = canonicalize_request(
        {
            "model": "demo",
            "system": [{"type": "text", "text": "At 2026-11-15T09:12:13Z"}],
            "messages": [],
        }
    )
    assert diagnose_requests([anthropic_a, anthropic_b]).findings[0].path == (
        "system[0].text"
    )
    openai_a = canonicalize_request(
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "At 2026-11-14T09:12:13Z"}],
                }
            ],
        }
    )
    openai_b = canonicalize_request(
        {
            "model": "demo",
            "response_format": {},
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "At 2026-11-15T09:12:13Z"}],
                }
            ],
        }
    )
    assert diagnose_requests([openai_a, openai_b]).findings[0].path == (
        "messages[0].content[0].text"
    )


def test_unsupported_content_and_context_are_not_candidate_causes() -> None:
    before = canonicalize_request(
        {
            "model": "demo",
            "response_format": {},
            "messages": [{"role": "user", "content": [{"type": "image", "url": "a"}]}],
        }
    )
    after = canonicalize_request(
        {
            "model": "demo",
            "response_format": {},
            "messages": [{"role": "user", "content": [{"type": "image", "url": "b"}]}],
        }
    )
    result = diagnose_requests([before, after])
    assert [(item.code, item.evidence) for item in result.findings] == [
        ("coverage_limited", "limited")
    ]
    assert "image" not in repr(result)

    context_a = canonicalize_request(
        {"model": "demo", "system": "Same", "messages": [], "temperature": 0}
    )
    context_b = canonicalize_request(
        {"model": "demo", "system": "Same", "messages": [], "temperature": 1}
    )
    assert _codes(context_a, context_b) == ["context_changed"]
    assert _codes(context_a, context_a) == []


def test_single_request_mixed_cohort_and_work_bounds() -> None:
    request = _request("anthropic", "A")
    alone = diagnose_requests([request])
    assert [item.code for item in alone.findings] == ["insufficient_data"]
    with pytest.raises(InvalidRequestError, match="at least one"):
        diagnose_requests([])
    with pytest.raises(IncomparableRequestsError):
        diagnose_requests([request, _request("openai", "A")])
    with pytest.raises(InvalidRequestError, match="request limit"):
        diagnose_requests([request, request], limits=DiagnosisLimits(max_requests=1))
    with pytest.raises(InvalidRequestError, match="segment limit"):
        diagnose_requests([request], limits=DiagnosisLimits(max_segments=1))
    with pytest.raises(InvalidRequestError, match="node limit"):
        diagnose_requests([request], limits=DiagnosisLimits(max_nodes=1))
    with pytest.raises(InvalidRequestError, match="text limit"):
        diagnose_requests([request], limits=DiagnosisLimits(max_text_chars=1))
    with pytest.raises(InvalidRequestError, match="work limit"):
        diagnose_requests([request, request], limits=DiagnosisLimits(max_work_units=1))
    with pytest.raises(InvalidRequestError, match="work limit"):
        diagnose_requests(
            [request, _request("anthropic", "A" * 1_000)],
            limits=DiagnosisLimits(max_work_units=500),
        )
    with pytest.raises(InvalidRequestError, match="node limit"):
        diagnose_requests(
            [replace(request, context={str(i): i for i in range(100)})],
            limits=DiagnosisLimits(max_nodes=50),
        )
    with pytest.raises(InvalidRequestError, match="node limit"):
        diagnose_requests(
            [replace(request, context={"nested": list(range(100))})],
            limits=DiagnosisLimits(max_nodes=50),
        )
    with pytest.raises(InvalidRequestError, match="positive integers"):
        DiagnosisLimits(max_requests=True)
    with pytest.raises(InvalidRequestError, match="expected diagnosis limits"):
        diagnose_requests([request], limits=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="expected canonical"):
        diagnose_requests([request, object()])  # type: ignore[list-item]
    with pytest.raises(InvalidRequestError, match="invalid canonical segment path"):
        diagnose_requests(
            [replace(request, segments=(replace(request.segments[0], path=_CANARY),))]
        )


def test_deterministic_ranking_counts_and_private_result() -> None:
    first = _request("anthropic", f"{_CANARY} 2026-11-14T09:12:13Z", tail=2)
    second = _request("anthropic", f"{_CANARY} 2026-11-15T09:12:13Z", tail=2)
    third = _request("anthropic", f"{_CANARY} 2026-11-16T09:12:13Z", tail=2)
    result = diagnose_requests([first, second, third])
    assert result == diagnose_requests([first, second, third])
    assert [(item.code, item.count) for item in result.findings] == [
        ("volatile_timestamp", 2),
        ("stable_suffix_after_drift", 2),
    ]
    assert all(
        _CANARY not in item.explanation + item.action for item in result.findings
    )
    assert _CANARY not in repr(result)
