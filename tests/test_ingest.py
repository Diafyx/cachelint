"""Synthetic request fixtures for offline ingestion and privacy boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from cachelint import (
    CanonicalRequest,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    Segment,
    SourceLocation,
    canonicalize_request,
)
from cachelint._ingest import read_jsonl


def _body(**fields: object) -> dict[str, object]:
    return {
        "model": "demo",
        "messages": [{"role": "user", "content": "Hello"}],
        **fields,
    }


def test_anthropic_segment_order_and_original_mutation() -> None:
    tools = [{"name": "first"}, {"name": "second"}]
    body = _body(
        tools=tools,
        system=[{"type": "text", "text": "Rules"}, {"type": "image", "data": "secret"}],
        cache_control={"type": "ephemeral"},
    )
    result = canonicalize_request(body)
    assert isinstance(result, CanonicalRequest)
    assert result.provider == "anthropic"
    assert result.model == "demo"
    assert [(part.path, part.kind) for part in result.segments] == [
        ("tools", "presence"),
        ("tools[0]", "tool"),
        ("tools[1]", "tool"),
        ("system", "presence"),
        ("system[0]", "block"),
        ("system[1]", "block"),
        ("messages[0]", "message"),
    ]
    assert result.coverage_warnings == ("opaque_content",)
    assert "cache_control" in result.context
    tools.reverse()
    assert result.segments[1].value == (
        "object",
        (("name", ("string", "first")),),
    )


def test_openai_segments_preserve_content_shape_and_tool_order() -> None:
    base = _body(
        response_format={"type": "json_object"},
        tools=[{"name": "a"}, {"name": "b"}],
    )
    reordered = _body(
        response_format={"type": "json_object"},
        tools=[{"name": "b"}, {"name": "a"}],
    )
    result = canonicalize_request(base)
    assert result.provider == "openai"
    assert [segment.path for segment in result.segments] == [
        "tools",
        "tools[0]",
        "tools[1]",
        "messages[0]",
    ]
    assert result.segments != canonicalize_request(reordered).segments
    block_form = _body(
        messages=[{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]
    )
    assert (
        canonicalize_request(block_form, provider="openai").segments
        != canonicalize_request(_body(), provider="openai").segments
    )

    multiline = _body(messages=[{"role": "user", "content": "Line one\nLine two"}])
    assert canonicalize_request(multiline, provider="openai").segments[-1].value == (
        "object",
        (
            ("content", ("string", "Line one\nLine two")),
            ("role", ("string", "user")),
        ),
    )


def test_json_equivalence_and_type_distinctions() -> None:
    left = _body(
        system="",
        tools=[{"name": "demo", "schema": {"a": 1, "b": [True, None]}}],
    )
    right = _body(
        system="",
        tools=[{"schema": {"b": [True, None], "a": 1.0}, "name": "demo"}],
    )
    assert canonicalize_request(left).segments == canonicalize_request(right).segments
    assert (
        canonicalize_request(left).segments
        != canonicalize_request(
            _body(
                system="",
                tools=[{"name": "demo", "schema": {"a": True, "b": [True, None]}}],
            )
        ).segments
    )
    assert (
        canonicalize_request(_body(), provider="anthropic").segments
        != canonicalize_request(_body(system=""), provider="anthropic").segments
    )
    assert (
        canonicalize_request(_body(system=""), provider="anthropic").segments
        != canonicalize_request(_body(system=[]), provider="anthropic").segments
    )
    assert (
        canonicalize_request(_body(tools=[]), provider="openai").segments
        != canonicalize_request(_body(), provider="openai").segments
    )


def test_context_and_coverage_warnings() -> None:
    body = _body(
        response_format=None,
        future_mode={"enabled": True},
        messages=[
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello", "future": 1}],
                "future_message_field": "secret",
            }
        ],
    )
    result = canonicalize_request(body)
    assert result.coverage_warnings == ("opaque_content", "unmodeled_context")
    assert result.context["future_mode"] == (
        "object",
        (("enabled", ("bool", True)),),
    )
    assert "future_mode" not in str(result.coverage_warnings)
    assert result.segments[0].path == "messages[0]"


@pytest.mark.parametrize(
    ("body", "provider", "expected"),
    [
        (_body(), None, "ambiguous provider"),
        (
            _body(system=None, response_format=None),
            None,
            "conflicting provider markers",
        ),
        (_body(system=None), "openai", "provider conflicts"),
        (_body(response_format=None), "anthropic", "provider conflicts"),
        (_body(), "unknown", "unsupported provider"),
    ],
)
def test_provider_rejections(
    body: dict[str, object], provider: object, expected: str
) -> None:
    with pytest.raises(InvalidRequestError, match=expected):
        canonicalize_request(body, provider=cast("Provider | None", provider))


def test_provider_markers_use_presence_and_explicit_provider() -> None:
    assert (
        canonicalize_request(_body(cache_control=None), provider="anthropic").provider
        == "anthropic"
    )
    assert (
        canonicalize_request(_body(response_format=None), provider="openai").provider
        == "openai"
    )
    assert canonicalize_request(_body(), provider="openai").provider == "openai"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ([], "request must be a JSON object"),
        ({1: "secret"}, "JSON object keys must be strings"),
        (_body(future=object()), "non-JSON value"),
        (_body(temperature=float("nan")), "invalid JSON number"),
        (_body(model=""), "model must be a nonempty string"),
        (_body(model=1), "model must be a nonempty string"),
        ({"model": "demo"}, "messages must be an array"),
        (_body(messages={}), "messages must be an array"),
        (_body(tools=None), "tools must be an array"),
        (_body(system=1), "system must be a string or array"),
        (_body(messages=["secret"]), "messages entries must be JSON objects"),
        (_body(messages=[{"content": "secret"}]), "message requires a string role"),
        (_body(messages=[{"role": "user"}]), "message requires a string role"),
    ],
)
def test_invalid_shapes_do_not_echo_values(body: object, expected: str) -> None:
    with pytest.raises(InvalidRequestError, match=expected) as error:
        canonicalize_request(cast("Mapping[str, object]", body), provider="anthropic")
    assert "secret" not in str(error.value)


def test_cyclic_value_is_rejected() -> None:
    loop: list[object] = []
    loop.append(loop)
    with pytest.raises(InvalidRequestError, match="cyclic request value"):
        canonicalize_request(_body(tools=loop), provider="openai")


def test_minimum_shape_keeps_unusual_nested_values_with_warning() -> None:
    body = _body(
        tools=["opaque tool"],
        system=["opaque block"],
        messages=[
            {"role": "assistant", "content": None},
            {"role": "user", "content": ["opaque block"]},
        ],
    )
    result = canonicalize_request(body, provider="anthropic")
    assert result.coverage_warnings == ("opaque_content",)
    assert result.segments[1].value == ("string", "opaque tool")
    assert result.segments[-2].value == (
        "object",
        (("content", ("null",)), ("role", ("string", "assistant"))),
    )


def test_extreme_python_values_get_sanitized_errors() -> None:
    huge_integer = 10**5000
    with pytest.raises(InvalidRequestError, match="invalid JSON number"):
        canonicalize_request(_body(temperature=huge_integer), provider="openai")

    nested: list[object] = []
    current = nested
    for _ in range(1500):
        child: list[object] = []
        current.append(child)
        current = child
    with pytest.raises(InvalidRequestError, match="request nesting is too deep"):
        canonicalize_request(_body(future=nested), provider="openai")


def test_source_location_and_public_segments() -> None:
    assert str(SourceLocation("requests.jsonl", 3)) == "requests.jsonl:3"
    assert str(SourceLocation("requests.jsonl")) == "requests.jsonl"
    assert isinstance(Segment("system", "text", ("string", "safe")), Segment)
    error = IncomparableRequestsError(
        "mixed model", SourceLocation("requests.jsonl", 3)
    )
    assert error.source == SourceLocation("requests.jsonl", 3)
    assert str(error) == "requests.jsonl:3: mixed model"


def test_jsonl_reader_counts_physical_lines_and_accepts_initial_bom(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_bytes(
        b'\xef\xbb\xbf{"model":"demo","system":"Rule","messages":[]}\n'
        b"  \n"
        b'{"messages":[],"model":"demo","system":"Rule"}\n'
    )
    requests = read_jsonl(path)
    assert len(requests) == 2
    assert requests[0].segments == requests[1].segments
    assert requests[0].provider == "anthropic"


@pytest.mark.parametrize(
    ("content", "line", "reason"),
    [
        ('{"model":"demo","system":"secret","messages":[]}\n{', 2, "invalid JSON"),
        (
            '{"model":"demo","system":"secret","model":"demo","messages":[]}',
            1,
            "duplicate JSON object key",
        ),
        (
            '{"model":"demo","system":"secret","messages":[],"temperature":NaN}',
            1,
            "invalid JSON number",
        ),
        ("[]", 1, "request must be a JSON object"),
        (
            '{"model":"demo","messages":[]}',
            1,
            "ambiguous provider; specify anthropic or openai",
        ),
        (
            '\ufeff{"model":"demo","system":"secret","messages":[]}\n\ufeff{}',
            2,
            "invalid JSON",
        ),
    ],
)
def test_jsonl_reader_reports_safe_line_error(
    tmp_path: Path, content: str, line: int, reason: str
) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(InvalidRequestError) as error:
        read_jsonl(path)
    assert error.value.source == SourceLocation(str(path), line)
    assert error.value.reason == reason
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        '{"model":"demo","system":"A","messages":[]}\n{"model":"other","system":"A","messages":[]}',
        '{"model":"demo","system":"A","messages":[]}\n{"model":"demo","response_format":{},"messages":[]}',
    ],
)
def test_jsonl_reader_rejects_mixed_comparison_inputs(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(IncomparableRequestsError) as error:
        read_jsonl(path)
    assert error.value.source == SourceLocation(str(path), 2)
    assert error.value.reason == "requests must use the same provider and model"


def test_jsonl_reader_empty_invalid_utf8_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_text(" \n\n", encoding="utf-8")
    with pytest.raises(InvalidRequestError, match="empty input") as empty:
        read_jsonl(path)
    assert empty.value.source == SourceLocation(str(path))
    path.write_bytes(b"\xff")
    with pytest.raises(InvalidRequestError, match="invalid UTF-8"):
        read_jsonl(path)
    path.unlink()
    with pytest.raises(InvalidRequestError, match="unreadable input file"):
        read_jsonl(path)


def test_decimal_json_values_are_compared_by_value(tmp_path: Path) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_text(
        '{"model":"demo","messages":[],"response_format":{},"temperature":1}\n'
        '{"model":"demo","messages":[],"response_format":{},"temperature":1.0}\n',
        encoding="utf-8",
    )
    first, second = read_jsonl(path)
    assert first.context["temperature"] == second.context["temperature"]
    assert first.context["temperature"] == ("number", Decimal(1))
