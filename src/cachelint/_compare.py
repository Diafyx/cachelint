"""Deterministic, content-safe comparisons of canonical request bodies."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from cachelint._estimate import EstimateMethod, PrefixEstimate, estimate_prefix
from cachelint._ingest import (
    _KNOWN_CONTEXT,
    CanonicalRequest,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    Segment,
)

PromptStatus = Literal["identical", "diverged", "insufficient_data"]
DivergenceKind = Literal[
    "text_changed",
    "value_changed",
    "type_changed",
    "object_changed",
    "inserted",
    "removed",
]
FindingCode = Literal[
    "prompt_identical",
    "prompt_text_changed",
    "prompt_structure_changed",
    "prompt_inserted",
    "prompt_removed",
    "context_changed",
    "coverage_limited",
    "insufficient_data",
]

_SAFE_SEGMENT_PATH = re.compile(
    r"(?:tools(?:\[\d+\])?|system(?:\[\d+\])?|messages\[\d+\])\Z"
)
_FIELD_ORDER = ("role", "content", "name", "tool_call_id", "tool_calls")
_BLOCK_FIELD_ORDER = ("type", "text", "cache_control", "prompt_cache_breakpoint")
_FINDING_TEXT: dict[FindingCode, str] = {
    "prompt_identical": (
        "Modeled prompt segments match; this does not guarantee a provider cache hit."
    ),
    "prompt_text_changed": (
        "Text changes at the reported offset in the modeled prompt prefix."
    ),
    "prompt_structure_changed": (
        "A prompt component changes at the reported path; later components "
        "are outside the common prefix."
    ),
    "prompt_inserted": (
        "A prompt component appears after the common prefix relative to request one."
    ),
    "prompt_removed": (
        "A prompt component is absent after the common prefix relative to request one."
    ),
    "context_changed": (
        "Request context differs; provider cache behavior cannot be inferred "
        "from prompt comparison alone."
    ),
    "coverage_limited": (
        "Some content or context is not fully modeled; review coverage warnings."
    ),
    "insufficient_data": "At least two requests are needed to compare prompt prefixes.",
}


@dataclass(frozen=True)
class Divergence:
    """The first safe structural difference; no request values are retained."""

    path: str
    kind: DivergenceKind
    offset: int | None = None


@dataclass(frozen=True)
class ContextDifference:
    """A fixed field category, or the generic unmodeled_context category."""

    category: str


@dataclass(frozen=True)
class Finding:
    """A fixed explanation of observed evidence, never a cache-hit prediction."""

    code: FindingCode
    message: str


@dataclass(frozen=True)
class ComparisonResult:
    """Content-safe summary of a group of canonical requests."""

    provider: Provider
    request_count: int
    prompt_status: PromptStatus
    common_prefix_segments: int
    first_divergence: Divergence | None
    context_differences: tuple[ContextDifference, ...]
    coverage_warnings: tuple[str, ...]
    findings: tuple[Finding, ...]
    prefix_estimate: PrefixEstimate | None = None


def _tag(value: object) -> str | None:
    if isinstance(value, tuple) and value and isinstance(value[0], str):
        return value[0]
    return None


def _payload(value: object) -> object:
    return cast(tuple[object, object], value)[1]


def _fields(value: object) -> dict[str, object] | None:
    if _tag(value) != "object":
        return None
    return dict(cast(tuple[tuple[str, object], ...], _payload(value)))


def _array(value: object) -> tuple[object, ...]:
    return cast(tuple[object, ...], _payload(value))


def _generic_difference(left: object, right: object, path: str) -> Divergence:
    if _tag(left) != _tag(right):
        return Divergence(path, "type_changed")
    if _tag(left) == "object":
        return Divergence(path, "object_changed")
    return Divergence(path, "value_changed")


def _text_difference(left: object, right: object, path: str) -> Divergence:
    if _tag(left) != "string" or _tag(right) != "string":
        return _generic_difference(left, right, path)
    before = cast(str, _payload(left))
    after = cast(str, _payload(right))
    offset = next(
        (
            index
            for index, pair in enumerate(zip(before, after, strict=False))
            if pair[0] != pair[1]
        ),
        min(len(before), len(after)),
    )
    return Divergence(path, "text_changed", offset)


def _block_difference(left: object, right: object, path: str) -> Divergence:
    before = _fields(left)
    after = _fields(right)
    if before is None or after is None:
        return _generic_difference(left, right, path)
    if before.get("type") != ("string", "text") or after.get("type") != (
        "string",
        "text",
    ):
        return Divergence(path, "object_changed")
    for field in _BLOCK_FIELD_ORDER:
        if field not in before and field not in after:
            continue
        child_path = f"{path}.{field}"
        if field not in before:
            return Divergence(child_path, "inserted")
        if field not in after:
            return Divergence(child_path, "removed")
        if before[field] != after[field]:
            if field == "text":
                return _text_difference(before[field], after[field], child_path)
            return _generic_difference(before[field], after[field], child_path)
    return Divergence(path, "object_changed")


def _block_array_difference(left: object, right: object, path: str) -> Divergence:
    before = _array(left)
    after = _array(right)
    for index in range(min(len(before), len(after))):
        if before[index] == after[index]:
            continue
        child_path = f"{path}[{index}]"
        if len(after) > len(before) and after[index + 1] == before[index]:
            return Divergence(child_path, "inserted")
        if len(before) > len(after) and before[index + 1] == after[index]:
            return Divergence(child_path, "removed")
        return _block_difference(before[index], after[index], child_path)
    if len(after) > len(before):
        return Divergence(f"{path}[{len(before)}]", "inserted")
    return Divergence(f"{path}[{len(after)}]", "removed")


def _content_difference(left: object, right: object, path: str) -> Divergence:
    if _tag(left) == _tag(right) == "string":
        return _text_difference(left, right, path)
    if _tag(left) == _tag(right) == "array":
        return _block_array_difference(left, right, path)
    return _generic_difference(left, right, path)


def _message_difference(left: object, right: object, path: str) -> Divergence:
    before = cast(dict[str, object], _fields(left))
    after = cast(dict[str, object], _fields(right))
    for field in _FIELD_ORDER:
        if field not in before and field not in after:
            continue
        child_path = f"{path}.{field}"
        if field not in before:
            return Divergence(child_path, "inserted")
        if field not in after:
            return Divergence(child_path, "removed")
        if before[field] != after[field]:
            if field == "content":
                return _content_difference(before[field], after[field], child_path)
            return _generic_difference(before[field], after[field], child_path)
    return Divergence(path, "object_changed")


def _segment_difference(left: Segment, right: Segment) -> Divergence:
    path = left.path
    if left.kind != right.kind:
        return Divergence(path, "type_changed")
    if left.kind == "text":
        return _text_difference(left.value, right.value, path)
    if left.kind == "message":
        return _message_difference(left.value, right.value, path)
    if left.kind == "block":
        return _block_difference(left.value, right.value, path)
    if left.kind == "presence":
        return Divergence(path, "type_changed")
    return _generic_difference(left.value, right.value, path)


def _group_rank(path: str) -> int:
    if path.startswith("tools"):
        return 0
    if path.startswith("system"):
        return 1
    return 2


def _group_count(segments: tuple[Segment, ...], kind: str) -> int:
    return sum(segment.kind == kind for segment in segments)


def _pair_difference(
    before: tuple[Segment, ...], after: tuple[Segment, ...]
) -> tuple[int, Divergence] | None:
    for index in range(min(len(before), len(after))):
        left = before[index]
        right = after[index]
        if left == right:
            continue
        if left.path != right.path:
            if _group_rank(left.path) < _group_rank(right.path):
                return index, Divergence(left.path, "removed")
            if _group_rank(left.path) > _group_rank(right.path):
                return index, Divergence(right.path, "inserted")
            return index, Divergence(left.path, "value_changed")
        if left.kind == right.kind and left.kind in ("tool", "message", "block"):
            left_count = _group_count(before, left.kind)
            right_count = _group_count(after, right.kind)
            if (
                right_count > left_count
                and index + 1 < len(after)
                and after[index + 1].kind == left.kind
                and after[index + 1].value == left.value
            ):
                return index, Divergence(right.path, "inserted")
            if (
                left_count > right_count
                and index + 1 < len(before)
                and before[index + 1].kind == right.kind
                and before[index + 1].value == right.value
            ):
                return index, Divergence(left.path, "removed")
        return index, _segment_difference(left, right)
    if len(after) > len(before):
        return len(before), Divergence(after[len(before)].path, "inserted")
    if len(before) > len(after):
        return len(after), Divergence(before[len(after)].path, "removed")
    return None


def _context_differences(
    requests: tuple[CanonicalRequest, ...],
) -> tuple[ContextDifference, ...]:
    reference = requests[0].context
    changed: set[str] = set()
    absent = object()
    for request in requests[1:]:
        for key in reference.keys() | request.context.keys():
            if reference.get(key, absent) != request.context.get(key, absent):
                changed.add(key if key in _KNOWN_CONTEXT else "unmodeled_context")
    return tuple(ContextDifference(category) for category in sorted(changed))


def _findings(
    status: PromptStatus,
    divergence: Divergence | None,
    context: tuple[ContextDifference, ...],
    warnings: tuple[str, ...],
) -> tuple[Finding, ...]:
    codes: list[FindingCode] = []
    if status == "insufficient_data":
        codes.append("insufficient_data")
    elif status == "identical":
        codes.append("prompt_identical")
    else:
        kind = cast(Divergence, divergence).kind
        if kind == "text_changed":
            codes.append("prompt_text_changed")
        elif kind == "inserted":
            codes.append("prompt_inserted")
        elif kind == "removed":
            codes.append("prompt_removed")
        else:
            codes.append("prompt_structure_changed")
    if context:
        codes.append("context_changed")
    if warnings:
        codes.append("coverage_limited")
    return tuple(Finding(code, _FINDING_TEXT[code]) for code in codes)


def compare_requests(
    requests: Sequence[CanonicalRequest],
    *,
    estimate_method: EstimateMethod = "heuristic",
) -> ComparisonResult:
    """Find the earliest modeled prefix difference across one or more requests."""
    if estimate_method not in ("heuristic", "tiktoken"):
        raise InvalidRequestError("unsupported estimate method")
    group = tuple(requests)
    if not group:
        raise InvalidRequestError("at least one canonical request is required")
    if any(not isinstance(request, CanonicalRequest) for request in group):
        raise InvalidRequestError("expected canonical requests")
    reference = group[0]
    for request in group:
        if request.provider not in ("anthropic", "openai"):
            raise InvalidRequestError("unsupported canonical provider")
        if request.provider != reference.provider or request.model != reference.model:
            raise IncomparableRequestsError(
                "requests must use the same provider and model"
            )
        if any(
            not _SAFE_SEGMENT_PATH.fullmatch(segment.path)
            for segment in request.segments
        ):
            raise InvalidRequestError("invalid canonical segment path")
    context = _context_differences(group)
    warnings = tuple(
        sorted(
            warning
            for warning in {
                item for request in group for item in request.coverage_warnings
            }
            if warning in ("opaque_content", "unmodeled_context")
        )
    )
    if len(group) == 1:
        status: PromptStatus = "insufficient_data"
        divergence = None
        common = 0
        prefix_estimate = None
    else:
        differences = [
            difference
            for request in group[1:]
            if (difference := _pair_difference(reference.segments, request.segments))
            is not None
        ]
        first = min(differences, key=lambda item: item[0], default=None)
        status = "diverged" if first is not None else "identical"
        common = first[0] if first is not None else len(reference.segments)
        divergence = first[1] if first is not None else None
        partial: tuple[str, int] | None = None
        if first is not None and first[1].kind == "text_changed":
            at_first = [item[1] for item in differences if item[0] == common]
            if all(
                item.kind == "text_changed"
                and item.path == first[1].path
                and item.offset is not None
                for item in at_first
            ):
                partial = (
                    first[1].path,
                    min(cast(int, item.offset) for item in at_first),
                )
        prefix_estimate = estimate_prefix(reference, common, partial, estimate_method)
    return ComparisonResult(
        reference.provider,
        len(group),
        status,
        common,
        divergence,
        context,
        warnings,
        _findings(status, divergence, context, warnings),
        prefix_estimate,
    )
