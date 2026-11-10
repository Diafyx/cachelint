"""Ranked corpus findings use bounded observations and safe public fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cachelint._corpus as corpus_module
from cachelint import (
    CorpusLimits,
    CorpusPattern,
    Divergence,
    Segment,
    analyze_corpus,
    canonicalize_request,
    compare_requests,
)
from cachelint._compare import _pair_difference


def _body(**changes: object) -> dict[str, object]:
    return {
        "model": "private-model-name",
        "tools": [{"name": "private-tool-name"}],
        "system": "private-system-baseline",
        "messages": [{"role": "user", "content": "private-message-baseline"}],
        **changes,
    }


def _write(path: Path, *bodies: dict[str, object]) -> Path:
    path.write_text(
        "".join(json.dumps(body) + "\n" for body in bodies), encoding="utf-8"
    )
    return path


def test_ranked_patterns_count_duplicates_context_and_coverage(tmp_path: Path) -> None:
    reference = _body()
    tool = _body(tools=[{"name": "private-tool-changed"}])
    system = _body(system="private-system-changed")
    message = _body(messages=[{"role": "user", "content": "private-message-changed"}])
    path = _write(
        tmp_path / "requests.jsonl",
        reference,
        tool,
        tool,
        tool,
        system,
        _body(system="private-system-other", private_unknown_key="secret"),
        message,
        _body(max_tokens=128),
        reference,
    )
    result = analyze_corpus(path)
    assert result == analyze_corpus(path)
    group = result.groups[0]
    assert (
        group.paired_count,
        group.identical_count,
        group.diverged_count,
        group.context_changed_count,
        group.coverage_limited_count,
    ) == (8, 2, 6, 2, 1)
    assert [
        (item.rank, item.category, item.path, item.count) for item in result.patterns
    ] == [
        (1, "tools", "tools[0]", 3),
        (2, "system", "system", 2),
        (3, "messages", "messages[0].content", 1),
    ]
    assert result.patterns[0].kind == "object_changed"
    assert result.patterns[1].kind == "text_changed"
    assert result.patterns[1].coverage_limited_count == 1
    assert result.patterns[1].context_changed_count == 1
    assert result.patterns[1].text_offset_min is not None
    assert result.patterns[1].text_offset_max is not None
    assert result.provider_reported_cache_status == "unavailable"
    assert all(isinstance(pattern, CorpusPattern) for pattern in result.patterns)
    for secret in (
        "private-model-name",
        "private-tool-name",
        "private-system-baseline",
        "private-message-baseline",
        "secret",
    ):
        assert secret not in repr(result)


def test_file_and_line_order_choose_reference_not_input_order(
    tmp_path: Path,
) -> None:
    first = _write(tmp_path / "a.jsonl", _body(system="first"))
    second = _write(tmp_path / "z.jsonl", _body(system="second"), _body(system="third"))
    left = analyze_corpus([second, first])
    right = analyze_corpus([first, second])
    assert left == right
    assert left.groups[0].paired_count == 2
    assert left.patterns[0].path == "system"
    assert left.patterns[0].count == 2
    expected = compare_requests(
        [
            canonicalize_request(_body(system="first")),
            canonicalize_request(_body(system="second")),
        ]
    )
    assert left.patterns[0].prefix_estimate == expected.prefix_estimate


def test_ties_use_prefix_then_cohort_and_safe_path(tmp_path: Path) -> None:
    first = _body(model="model-a")
    second = _body(model="model-b")
    path = _write(
        tmp_path / "mixed.jsonl",
        first,
        second,
        _body(model="model-a", system="changed"),
        _body(model="model-b", tools=[{"name": "changed"}]),
        _body(model="model-a", messages=[{"role": "user", "content": "changed"}]),
    )
    result = analyze_corpus(path)
    assert [
        (item.rank, item.group_index, item.category) for item in result.patterns
    ] == [
        (1, 2, "tools"),
        (2, 1, "system"),
        (3, 1, "messages"),
    ]
    assert [group.request_count for group in result.groups] == [3, 2]


def test_text_offset_range_and_earliest_offset_estimate(tmp_path: Path) -> None:
    baseline = _body(system="prefix-abcd")
    early = _body(system="prefix-zbcd")
    late = _body(system="prefix-azcd")
    path = _write(tmp_path / "text.jsonl", baseline, late, early)
    pattern = analyze_corpus(path).patterns[0]
    assert (pattern.text_offset_min, pattern.text_offset_max) == (7, 8)
    expected = compare_requests(
        [canonicalize_request(baseline), canonicalize_request(early)]
    )
    assert pattern.prefix_estimate == expected.prefix_estimate


def test_identical_and_single_request_have_no_divergence_pattern(
    tmp_path: Path,
) -> None:
    duplicate = _body()
    one = _write(tmp_path / "one.jsonl", duplicate)
    single = analyze_corpus(one)
    assert single.groups[0].comparison.prompt_status == "insufficient_data"
    assert single.groups[0].paired_count == 0
    assert single.patterns == ()
    _write(one, duplicate, duplicate)
    identical = analyze_corpus(one)
    assert identical.groups[0].identical_count == 1
    assert identical.groups[0].diverged_count == 0
    assert identical.patterns == ()


def test_pattern_pairing_is_linear_at_configured_request_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = [_body(system=f"value-{index}") for index in range(1000)]
    path = _write(tmp_path / "bounded.jsonl", *requests)
    calls = 0

    def count_pair(
        before: tuple[Segment, ...], after: tuple[Segment, ...]
    ) -> tuple[int, Divergence] | None:
        nonlocal calls
        calls += 1
        return _pair_difference(before, after)

    monkeypatch.setattr(corpus_module, "_pair_difference", count_pair)
    result = analyze_corpus(path, limits=CorpusLimits(max_requests=1000))
    assert calls == 999
    assert result.groups[0].paired_count == 999
    assert result.groups[0].diverged_count == 999
    assert sum(pattern.count for pattern in result.patterns) == 999
