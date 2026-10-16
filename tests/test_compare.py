"""Comparison examples use synthetic values and assert safe public results."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from cachelint import (
    CanonicalRequest,
    ComparisonResult,
    ContextDifference,
    Divergence,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    Segment,
    canonicalize_request,
    compare_requests,
)


def _anthropic(**fields: object) -> CanonicalRequest:
    body: dict[str, object] = {
        "model": "demo",
        "system": "Rules",
        "messages": [{"role": "user", "content": "Hello"}],
        **fields,
    }
    return canonicalize_request(body)


def _openai(**fields: object) -> CanonicalRequest:
    body: dict[str, object] = {
        "model": "demo",
        "response_format": {"type": "text"},
        "messages": [{"role": "user", "content": "Hello"}],
        **fields,
    }
    return canonicalize_request(body)


def test_identical_prompt_and_single_request() -> None:
    request = _openai()
    result = compare_requests([request, _openai()])
    assert isinstance(result, ComparisonResult)
    assert result.provider == "openai"
    assert result.request_count == 2
    assert result.prompt_status == "identical"
    assert result.common_prefix_segments == len(request.segments)
    assert result.first_divergence is None
    assert result.context_differences == ()
    assert result.coverage_warnings == ()
    assert [finding.code for finding in result.findings] == ["prompt_identical"]
    assert "does not guarantee" in result.findings[0].message

    one = compare_requests([request])
    assert one.prompt_status == "insufficient_data"
    assert one.request_count == 1
    assert one.common_prefix_segments == 0
    assert one.first_divergence is None
    assert [finding.code for finding in one.findings] == ["insufficient_data"]


def test_system_text_reports_unicode_code_point_offset() -> None:
    before = _anthropic(system="You are helpful.")
    after = _anthropic(system="You are careful.")
    result = compare_requests([before, after])
    assert result.prompt_status == "diverged"
    assert result.common_prefix_segments == 1
    assert result.first_divergence == Divergence("system", "text_changed", 8)
    assert result.findings[0].code == "prompt_text_changed"
    assert "helpful" not in repr(result)
    assert "careful" not in repr(result)

    ended = compare_requests([_anthropic(system="🙂a"), _anthropic(system="🙂")])
    assert ended.first_divergence == Divergence("system", "text_changed", 1)


def test_openai_message_text_and_role_differences() -> None:
    changed = compare_requests(
        [_openai(), _openai(messages=[{"role": "user", "content": "Help"}])]
    )
    assert changed.first_divergence == Divergence(
        "messages[0].content", "text_changed", 3
    )
    role = compare_requests(
        [_openai(), _openai(messages=[{"role": "assistant", "content": "Hello"}])]
    )
    assert role.first_divergence == Divergence("messages[0].role", "value_changed")


def test_tool_reorder_and_schema_keys_remain_private() -> None:
    left = _anthropic(tools=[{"name": "secret-a"}, {"name": "secret-b"}])
    right = _anthropic(tools=[{"name": "secret-b"}, {"name": "secret-a"}])
    result = compare_requests([left, right])
    assert result.first_divergence == Divergence("tools[0]", "object_changed")
    assert result.common_prefix_segments == 1
    assert "secret" not in repr(result)

    schema_left = _openai(tools=[{"input_schema": {"private-key": 1}}])
    schema_right = _openai(tools=[{"input_schema": {"private-key": 2}}])
    schema_result = compare_requests([schema_left, schema_right])
    assert schema_result.first_divergence == Divergence("tools[0]", "object_changed")
    assert "private-key" not in repr(schema_result)


def test_inserted_and_removed_tools_and_system_blocks() -> None:
    a = {"name": "A"}
    b = {"name": "B"}
    extra = {"name": "X"}
    assert compare_requests(
        [_anthropic(tools=[a, b]), _anthropic(tools=[extra, a, b])]
    ).first_divergence == Divergence("tools[0]", "inserted")
    assert compare_requests(
        [_anthropic(tools=[extra, a, b]), _anthropic(tools=[a, b])]
    ).first_divergence == Divergence("tools[0]", "removed")

    rule = {"type": "text", "text": "Rule"}
    added = {"type": "text", "text": "Extra"}
    assert compare_requests(
        [_anthropic(system=[rule]), _anthropic(system=[added, rule])]
    ).first_divergence == Divergence("system[0]", "inserted")
    assert compare_requests(
        [_anthropic(system=[added, rule]), _anthropic(system=[rule])]
    ).first_divergence == Divergence("system[0]", "removed")


def test_optional_presence_and_type_changes() -> None:
    absent = _openai()
    empty = _openai(tools=[])
    assert compare_requests([absent, empty]).first_divergence == Divergence(
        "tools", "inserted"
    )
    assert compare_requests([empty, absent]).first_divergence == Divergence(
        "tools", "removed"
    )
    assert compare_requests(
        [_anthropic(system=""), _anthropic(system=[])]
    ).first_divergence == Divergence("system", "type_changed")
    no_system = canonicalize_request(
        {"model": "demo", "messages": [{"role": "user", "content": "Hello"}]},
        provider="anthropic",
    )
    assert compare_requests([no_system, _anthropic()]).first_divergence == Divergence(
        "system", "inserted"
    )
    assert compare_requests([_anthropic(), no_system]).first_divergence == Divergence(
        "system", "removed"
    )


def test_message_insertions_removals_and_end_marker() -> None:
    a = {"role": "user", "content": "A"}
    b = {"role": "user", "content": "B"}
    x = {"role": "system", "content": "X"}
    reference = _openai(messages=[a, b])
    inserted = compare_requests([reference, _openai(messages=[x, a, b])])
    assert inserted.first_divergence == Divergence("messages[0]", "inserted")
    assert inserted.findings[0].code == "prompt_inserted"
    removed = compare_requests([_openai(messages=[x, a, b]), reference])
    assert removed.first_divergence == Divergence("messages[0]", "removed")
    assert removed.findings[0].code == "prompt_removed"
    assert compare_requests(
        [reference, _openai(messages=[a, b, x])]
    ).first_divergence == Divergence("messages[2]", "inserted")
    assert compare_requests(
        [reference, _openai(messages=[a])]
    ).first_divergence == Divergence("messages[1]", "removed")


def test_content_block_changes_and_text_offset() -> None:
    a = {"type": "text", "text": "Alpha"}
    b = {"type": "text", "text": "Beta"}
    x = {"type": "text", "text": "Extra"}
    base = _openai(messages=[{"role": "user", "content": [a, b]}])
    inserted = compare_requests(
        [base, _openai(messages=[{"role": "user", "content": [x, a, b]}])]
    )
    assert inserted.first_divergence == Divergence("messages[0].content[0]", "inserted")
    removed = compare_requests(
        [_openai(messages=[{"role": "user", "content": [x, a, b]}]), base]
    )
    assert removed.first_divergence == Divergence("messages[0].content[0]", "removed")
    assert compare_requests(
        [base, _openai(messages=[{"role": "user", "content": [a, b, x]}])]
    ).first_divergence == Divergence("messages[0].content[2]", "inserted")
    assert compare_requests(
        [base, _openai(messages=[{"role": "user", "content": [a]}])]
    ).first_divergence == Divergence("messages[0].content[1]", "removed")
    changed = compare_requests(
        [
            base,
            _openai(
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "Alpine"}, b]}
                ]
            ),
        ]
    )
    assert changed.first_divergence == Divergence(
        "messages[0].content[0].text", "text_changed", 3
    )


def test_system_blocks_and_opaque_content() -> None:
    text = {"type": "text", "text": "Rules"}
    changed_text = {"type": "text", "text": "Rulers"}
    assert compare_requests(
        [_anthropic(system=[text]), _anthropic(system=[changed_text])]
    ).first_divergence == Divergence("system[0].text", "text_changed", 4)

    image_before = _openai(
        messages=[{"role": "user", "content": [{"type": "image", "data": "secret-a"}]}]
    )
    image_after = _openai(
        messages=[{"role": "user", "content": [{"type": "image", "data": "secret-b"}]}]
    )
    opaque = compare_requests([image_before, image_after])
    assert opaque.first_divergence == Divergence(
        "messages[0].content[0]", "object_changed"
    )
    assert opaque.coverage_warnings == ("opaque_content",)
    assert [finding.code for finding in opaque.findings] == [
        "prompt_structure_changed",
        "coverage_limited",
    ]
    assert "secret" not in repr(opaque)


def test_text_block_metadata_and_opaque_block_values() -> None:
    text = {"type": "text", "text": "A"}
    with_control = {"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}}
    assert compare_requests(
        [_anthropic(system=[text]), _anthropic(system=[with_control])]
    ).first_divergence == Divergence("system[0].cache_control", "inserted")
    assert compare_requests(
        [_anthropic(system=[with_control]), _anthropic(system=[text])]
    ).first_divergence == Divergence("system[0].cache_control", "removed")
    changed = {"type": "text", "text": "A", "cache_control": {"type": "other"}}
    assert compare_requests(
        [_anthropic(system=[with_control]), _anthropic(system=[changed])]
    ).first_divergence == Divergence("system[0].cache_control", "object_changed")
    opaque = compare_requests(
        [
            _openai(messages=[{"role": "user", "content": ["secret-a"]}]),
            _openai(messages=[{"role": "user", "content": ["secret-b"]}]),
        ]
    )
    assert opaque.first_divergence == Divergence(
        "messages[0].content[0]", "value_changed"
    )
    assert opaque.coverage_warnings == ("opaque_content",)
    assert "secret" not in repr(opaque)

    nontext = compare_requests(
        [
            _openai(
                messages=[{"role": "user", "content": [{"type": "image", "data": "A"}]}]
            ),
            _openai(
                messages=[{"role": "user", "content": [{"type": "text", "text": "A"}]}]
            ),
        ]
    )
    assert nontext.first_divergence == Divergence(
        "messages[0].content[0]", "object_changed"
    )


def test_content_and_text_leaf_type_changes() -> None:
    content_type = compare_requests(
        [_openai(), _openai(messages=[{"role": "user", "content": None}])]
    )
    assert content_type.first_divergence == Divergence(
        "messages[0].content", "type_changed"
    )
    assert content_type.findings[0].code == "prompt_structure_changed"
    unusual_text = compare_requests(
        [
            _anthropic(system=[{"type": "text", "text": 1}]),
            _anthropic(system=[{"type": "text", "text": "A"}]),
        ]
    )
    assert unusual_text.first_divergence == Divergence("system[0].text", "type_changed")


def test_known_message_metadata_addition_and_removal() -> None:
    ordinary = _openai()
    named = _openai(
        messages=[{"role": "user", "content": "Hello", "name": "private-name"}]
    )
    assert compare_requests([ordinary, named]).first_divergence == Divergence(
        "messages[0].name", "inserted"
    )
    removed = compare_requests([named, ordinary])
    assert removed.first_divergence == Divergence("messages[0].name", "removed")
    assert "private-name" not in repr(removed)


def test_unknown_message_and_block_fields_use_safe_parent() -> None:
    left = _openai(
        messages=[{"role": "user", "content": "Hello", "private-id": "secret-a"}]
    )
    right = _openai(
        messages=[{"role": "user", "content": "Hello", "private-id": "secret-b"}]
    )
    result = compare_requests([left, right])
    assert result.first_divergence == Divergence("messages[0]", "object_changed")
    assert "private-id" not in repr(result)
    assert "secret" not in repr(result)

    before = _openai(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "A", "private": 1}]}
        ]
    )
    after = _openai(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "A", "private": 2}]}
        ]
    )
    assert compare_requests([before, after]).first_divergence == Divergence(
        "messages[0].content[0]", "object_changed"
    )


def test_context_differences_and_warnings_are_separate_from_prompt() -> None:
    first = _openai(prompt_cache_key="secret-one", future_mode="secret-one")
    second = _openai(prompt_cache_key="secret-two", future_mode="secret-two")
    result = compare_requests([first, second])
    assert result.prompt_status == "identical"
    assert result.first_divergence is None
    assert result.context_differences == (
        ContextDifference("prompt_cache_key"),
        ContextDifference("unmodeled_context"),
    )
    assert result.coverage_warnings == ("unmodeled_context",)
    assert [finding.code for finding in result.findings] == [
        "prompt_identical",
        "context_changed",
        "coverage_limited",
    ]
    assert "secret" not in repr(result)
    assert "future_mode" not in repr(result)

    matching_unknown = compare_requests([first, first])
    assert matching_unknown.context_differences == ()
    assert matching_unknown.coverage_warnings == ("unmodeled_context",)


def test_multiple_requests_choose_earliest_segment_then_input_order() -> None:
    first = _anthropic(tools=[{"name": "A"}])
    late = _anthropic(
        tools=[{"name": "A"}], messages=[{"role": "user", "content": "Help"}]
    )
    early = _anthropic(tools=[{"name": "B"}])
    result = compare_requests([first, late, early])
    assert result.request_count == 3
    assert result.common_prefix_segments == 1
    assert result.first_divergence == Divergence("tools[0]", "object_changed")

    tie = compare_requests(
        [_anthropic(system="A"), _anthropic(system="B"), _anthropic(system="C")]
    )
    assert tie.first_divergence == Divergence("system", "text_changed", 0)


@pytest.mark.parametrize("alteration", ["provider", "model"])
def test_incomparable_requests_are_sanitized(alteration: str) -> None:
    first = _openai()
    second = _anthropic() if alteration == "provider" else _openai(model="secret-model")
    with pytest.raises(IncomparableRequestsError) as error:
        compare_requests([first, second])
    assert error.value.reason == "requests must use the same provider and model"
    assert "secret" not in str(error.value)


def test_invalid_comparison_inputs_and_paths() -> None:
    with pytest.raises(InvalidRequestError, match="at least one"):
        compare_requests([])
    with pytest.raises(InvalidRequestError, match="expected canonical"):
        compare_requests(cast("list[CanonicalRequest]", [object()]))
    invalid_provider = replace(_openai(), provider=cast("Provider", "secret-provider"))
    with pytest.raises(InvalidRequestError, match="unsupported canonical provider"):
        compare_requests([invalid_provider, invalid_provider])
    forged = replace(
        _openai(),
        segments=(
            Segment("messages[0].private-secret", "message", ("string", "secret")),
        ),
    )
    with pytest.raises(InvalidRequestError, match="invalid canonical segment path"):
        compare_requests([forged, forged])


def test_manually_constructed_segments_still_keep_report_paths_safe() -> None:
    base = _openai()
    left = replace(base, segments=(Segment("tools[0]", "tool", "secret-a"),))
    right = replace(base, segments=(Segment("tools[0]", "tool", "secret-b"),))
    value = compare_requests([left, right])
    assert value.first_divergence == Divergence("tools[0]", "value_changed")
    assert "secret" not in repr(value)

    kind = compare_requests(
        [left, replace(base, segments=(Segment("tools[0]", "message", "secret-b"),))]
    )
    assert kind.first_divergence == Divergence("tools[0]", "type_changed")

    path = compare_requests(
        [left, replace(base, segments=(Segment("tools[1]", "tool", "secret-b"),))]
    )
    assert path.first_divergence == Divergence("tools[0]", "value_changed")
