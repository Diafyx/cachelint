"""Observable length guidance stays advisory and contains no prompt content."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from cachelint import (
    CanonicalRequest,
    InvalidRequestError,
    Provider,
    Segment,
    _estimate,
    canonicalize_request,
    compare_requests,
)


def _request(
    content: object,
    *,
    provider: str = "openai",
    model: str = "demo",
    **extra: object,
) -> CanonicalRequest:
    body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    return canonicalize_request(body, provider=cast(Provider, provider))


def _estimate_pair(
    left: CanonicalRequest, right: CanonicalRequest, method: str = "heuristic"
) -> _estimate.PrefixEstimate:
    result = compare_requests(
        [left, right],
        estimate_method=cast(_estimate.EstimateMethod, method),
    )
    assert result.prefix_estimate is not None
    assert "private" not in repr(result)
    return result.prefix_estimate


def test_plain_text_prefix_and_unknown_model() -> None:
    left = _request("private common prefix Alpha")
    right = _request("private common prefix Beta")
    estimate = _estimate_pair(left, right)
    assert estimate.method == "heuristic"
    assert estimate.tokens > 0
    assert estimate.minimum_cacheable_tokens is None
    assert estimate.minimum_comparison == "unknown"
    assert estimate.minimum_source is None
    assert estimate.rules_reviewed_on is None
    assert "wide" in estimate.uncertainty
    assert compare_requests([left]).prefix_estimate is None


def test_code_json_and_multibyte_content_change_heuristic() -> None:
    plain = _estimate_pair(_request("a" * 300 + "x"), _request("a" * 300 + "y"))
    code = _estimate_pair(
        _request('{"private": [1, true, null, "é"]}' + "x"),
        _request('{"private": [1, true, null, "é"]}' + "y"),
    )
    assert plain.tokens > code.tokens > 0
    assert (
        code.tokens
        == _estimate_pair(
            _request('{"private": [1, true, null, "é"]}' + "x"),
            _request('{"private": [1, true, null, "é"]}' + "y"),
        ).tokens
    )


def test_all_requests_limit_partial_prefix() -> None:
    result = compare_requests(
        [
            _request("abcdefgh X"),
            _request("abcdefgh Y"),
            _request("ab Z"),
        ]
    )
    short = _estimate_pair(_request("abcdefgh X"), _request("ab Z"))
    assert result.prefix_estimate == short


def test_mixed_differences_do_not_extend_common_text() -> None:
    first = _request("private common text")
    changed_text = _request("private different text")
    changed_role = canonicalize_request(
        {
            "model": "demo",
            "messages": [{"role": "assistant", "content": "private common text"}],
        },
        provider="openai",
    )
    result = compare_requests([first, changed_text, changed_role])
    assert result.common_prefix_segments == 0
    assert result.prefix_estimate is not None
    assert result.prefix_estimate.tokens == 0


def test_structured_values_and_text_blocks() -> None:
    tools = [{"name": "private", "schema": {"any": [1, True, None, "é"]}}]
    first = _request("one", tools=tools)
    second = _request("two", tools=tools)
    estimate = _estimate_pair(first, second)
    assert estimate.tokens > 0
    assert "private" not in repr(estimate)
    left = _request([{"type": "text", "text": "private hello X"}])
    right = _request([{"type": "text", "text": "private hello Y"}])
    assert _estimate_pair(left, right).tokens > 0
    a = _request(
        "private hello X",
        provider="anthropic",
        system=[{"type": "text", "text": "shared X"}],
    )
    b = _request(
        "private hello Y",
        provider="anthropic",
        system=[{"type": "text", "text": "shared Y"}],
    )
    assert _estimate_pair(a, b).tokens > 0


def test_known_model_minimum_is_advisory() -> None:
    short = _estimate_pair(
        _request("private A", model="gpt-5.6-sol"),
        _request("private B", model="gpt-5.6-sol"),
    )
    assert short.minimum_cacheable_tokens == 1024
    assert short.minimum_comparison == "estimate_below"
    assert short.rules_reviewed_on == "2026-10-24"
    assert (
        short.minimum_source
        == "https://developers.openai.com/api/docs/guides/prompt-caching"
    )
    long = _estimate_pair(
        _request("a" * 4096 + "X", model="gpt-5.6-sol"),
        _request("a" * 4096 + "Y", model="gpt-5.6-sol"),
    )
    assert long.minimum_comparison == "estimate_at_or_above"
    anthropic = _estimate_pair(
        _request("A", provider="anthropic", model="claude-opus-4-6"),
        _request("B", provider="anthropic", model="claude-opus-4-6"),
    )
    assert anthropic.minimum_cacheable_tokens == 4096


def test_tokenizer_present_absent_and_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("private A", model="gpt-5.6-sol")
    other = _request("private B", model="gpt-5.6-sol")
    original_import = importlib.import_module

    def absent(name: str) -> object:
        if name == "tiktoken":
            raise ImportError(name)
        return original_import(name)

    monkeypatch.setattr(importlib, "import_module", absent)
    with pytest.raises(InvalidRequestError, match="optional tiktoken is not installed"):
        _estimate_pair(request, other, "tiktoken")

    class Encoding:
        def encode_ordinary(self, value: str) -> list[int]:
            assert "private" in value
            return [1, 2, 3]

    fake = SimpleNamespace(encoding_for_model=lambda model: Encoding())
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: fake if name == "tiktoken" else original_import(name),
    )
    result = _estimate_pair(request, other, "tiktoken")
    assert result.tokens == 3
    assert result.method == "tiktoken"
    assert "provider framing" in result.uncertainty
    empty = _estimate_pair(_request("A"), _request("B"), "tiktoken")
    assert empty.tokens == 0

    def unknown(model: str) -> object:
        raise KeyError(model)

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(encoding_for_model=unknown)
            if name == "tiktoken"
            else original_import(name)
        ),
    )
    with pytest.raises(InvalidRequestError, match="no tiktoken encoding"):
        _estimate_pair(request, other, "tiktoken")
    with pytest.raises(InvalidRequestError, match="requires an OpenAI request"):
        _estimate_pair(
            _request("A", provider="anthropic"),
            _request("B", provider="anthropic"),
            "tiktoken",
        )


def test_invalid_estimate_method() -> None:
    with pytest.raises(InvalidRequestError, match="unsupported estimate method"):
        _estimate_pair(_request("A"), _request("B"), "unknown")
    with pytest.raises(InvalidRequestError, match="unsupported estimate method"):
        _estimate.estimate_prefix(
            _request("A"),
            0,
            None,
            cast(_estimate.EstimateMethod, "unknown"),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"reviewed_on": "invalid"},
        {"rules": None},
        {"rules": [None]},
        {
            "rules": [
                {
                    "provider": "other",
                    "model": "x",
                    "minimum_tokens": 1,
                    "source": "https://example.com",
                }
            ]
        },
        {
            "rules": [
                {
                    "provider": "openai",
                    "model": "x",
                    "minimum_tokens": 0,
                    "source": "https://example.com",
                }
            ]
        },
        {
            "rules": [
                {
                    "provider": "openai",
                    "model": "x",
                    "minimum_tokens": 1,
                    "source": "http://example.com",
                }
            ]
        },
        {
            "rules": [
                {
                    "provider": "openai",
                    "model": "x",
                    "minimum_tokens": 1,
                    "source": "https://example.com",
                }
            ]
            * 2
        },
    ],
)
def test_rule_data_rejects_invalid_changes(
    tmp_path: Path, change: dict[str, object]
) -> None:
    document: dict[str, object] = {
        "schema_version": 1,
        "reviewed_on": "2026-10-24",
        "rules": [],
    }
    document.update(change)
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="cache-rule"):
        _estimate._load_model_rules(path)


def test_rule_data_change_updates_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reviewed_on": "2026-10-25",
                "rules": [
                    {
                        "provider": "openai",
                        "model": "demo",
                        "minimum_tokens": 7,
                        "source": "https://example.com/rule",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _estimate, "files", lambda package: SimpleNamespace(joinpath=lambda name: path)
    )
    result = _estimate_pair(_request("private A"), _request("private B"))
    assert result.minimum_cacheable_tokens == 7
    assert result.rules_reviewed_on == "2026-10-25"
    assert result.minimum_source == "https://example.com/rule"


def test_unmodeled_shapes_are_sized_conservatively() -> None:
    assert _estimate._tag(None) is None
    assert _estimate._fields(("string", "x")) is None
    assert _estimate._render(("unmodeled",)) == ""
    assert (
        _estimate._partial_text(Segment("tools[0]", "tool", ("null",)), "tools[0]", 1)
        == ""
    )
    message = Segment(
        "messages[0]", "message", ("object", (("content", ("array", ())),))
    )
    assert _estimate._partial_text(message, "messages[0].role", 1) == ""
    assert _estimate._partial_text(message, "messages[0].content[0].text", 1) == ""
    nonarray = Segment(
        "messages[0]", "message", ("object", (("content", ("number", 1)),))
    )
    assert _estimate._partial_text(nonarray, "messages[0].content[0].text", 1) == ""
    invalid_block = Segment(
        "messages[0]", "message", ("object", (("content", ("array", (("null",),))),))
    )
    assert (
        _estimate._partial_text(invalid_block, "messages[0].content[0].text", 1) == ""
    )
    nontxt_block = Segment(
        "messages[0]",
        "message",
        (
            "object",
            (("content", ("array", (("object", (("text", ("number", 1)),)),))),),
        ),
    )
    assert _estimate._partial_text(nontxt_block, "messages[0].content[0].text", 1) == ""
