"""Bounded, content-safe candidate causes for modeled prompt-prefix drift."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from cachelint._compare import (
    _array,
    _fields,
    _pair_difference,
    _payload,
    compare_requests,
)
from cachelint._ingest import CanonicalRequest, InvalidRequestError, Provider, Segment

DiagnosisCode = Literal[
    "volatile_timestamp",
    "volatile_identifier",
    "tool_order_churn",
    "whitespace_drift",
    "stable_suffix_after_drift",
    "unclassified_drift",
    "context_changed",
    "coverage_limited",
    "insufficient_data",
]
EvidenceLevel = Literal["observed", "candidate", "limited"]

_TIMESTAMP = re.compile(
    r"(?<!\w)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})(?!\w)"
)
_IDENTIFIER = re.compile(
    r"(?<!\w)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}(?!\w)",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_CONTENT_BLOCK = re.compile(r"\[(\d+)\]\.text\Z")

_CODE_ORDER: tuple[DiagnosisCode, ...] = (
    "volatile_timestamp",
    "volatile_identifier",
    "tool_order_churn",
    "whitespace_drift",
    "stable_suffix_after_drift",
    "unclassified_drift",
    "context_changed",
    "coverage_limited",
    "insufficient_data",
)
_INFO: dict[DiagnosisCode, tuple[EvidenceLevel, str, str]] = {
    "volatile_timestamp": (
        "candidate",
        "Only valid ISO timestamp tokens changed in the first differing text.",
        "Review whether the changing timestamp can follow reusable instructions.",
    ),
    "volatile_identifier": (
        "candidate",
        "Only UUID-shaped tokens changed in the first differing text.",
        "Review whether the changing identifier can follow reusable instructions.",
    ),
    "tool_order_churn": (
        "observed",
        "The same tool definitions appear in a different array order.",
        "Keep tool order stable when building requests; review behavior before "
        "changing it.",
    ),
    "whitespace_drift": (
        "observed",
        "The first differing text matches after whitespace runs are collapsed.",
        "Make formatting deterministic if its exact spacing is not intentional.",
    ),
    "stable_suffix_after_drift": (
        "candidate",
        "At least two later prompt segments match after an earlier difference.",
        "Review whether changing content can follow the reusable prefix.",
    ),
    "unclassified_drift": (
        "limited",
        "The modeled prompt differs without a supported candidate cause.",
        "Inspect the private source requests and the first divergence.",
    ),
    "context_changed": (
        "observed",
        "Request context differs; the prompt comparison cannot explain its effect.",
        "Review changed request settings privately before attributing cache behavior.",
    ),
    "coverage_limited": (
        "limited",
        "Unsupported or opaque content limits this pair's diagnosis.",
        "Review coverage warnings and unsupported fields privately.",
    ),
    "insufficient_data": (
        "limited",
        "Only one request is available for this cohort.",
        "Collect another comparable request before diagnosing prefix drift.",
    ),
}


@dataclass(frozen=True)
class DiagnosisLimits:
    """Bound work across an already-canonicalized request cohort."""

    max_requests: int = 20_000
    max_segments: int = 200_000
    max_nodes: int = 2_000_000
    max_text_chars: int = 16 * 1024 * 1024
    max_work_units: int = 100_000_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_requests,
                self.max_segments,
                self.max_nodes,
                self.max_text_chars,
                self.max_work_units,
            )
        ):
            raise InvalidRequestError("diagnosis limits must be positive integers")


@dataclass(frozen=True)
class DiagnosisFinding:
    """A fixed explanation and action; no request values or excerpts."""

    code: DiagnosisCode
    evidence: EvidenceLevel
    path: str
    count: int
    explanation: str
    action: str


@dataclass(frozen=True)
class DiagnosisResult:
    """Content-free diagnoses relative to the cohort's first request."""

    provider: Provider
    request_count: int
    paired_count: int
    findings: tuple[DiagnosisFinding, ...]


def _measure_request(
    request: CanonicalRequest, *, max_nodes: int, max_chars: int
) -> tuple[int, int]:
    stack: list[object] = [request.model, request.context]
    for segment in request.segments:
        stack.extend((segment.path, segment.kind, segment.value))
    nodes = chars = 0
    while stack:
        value = stack.pop()
        nodes += 1
        if nodes + len(stack) > max_nodes:
            raise InvalidRequestError("diagnosis node limit exceeded")
        if isinstance(value, str):
            chars += len(value)
            if chars > max_chars:
                raise InvalidRequestError("diagnosis text limit exceeded")
        elif isinstance(value, Mapping):
            if nodes + len(stack) + len(value) * 2 > max_nodes:
                raise InvalidRequestError("diagnosis node limit exceeded")
            for key, item in value.items():
                stack.extend((key, item))
        elif isinstance(value, tuple | list):
            if nodes + len(stack) + len(value) > max_nodes:
                raise InvalidRequestError("diagnosis node limit exceeded")
            stack.extend(value)
    return nodes, chars


def _text_value(segment: Segment, path: str) -> str:
    """Read text only after the comparator has established a text difference."""
    value = segment.value
    if segment.kind == "text":
        return cast(str, _payload(value))
    if segment.kind == "block":
        fields = cast(dict[str, object], _fields(value))
        return cast(str, _payload(fields["text"]))
    fields = cast(dict[str, object], _fields(value))
    content = fields["content"]
    prefix = f"{segment.path}.content"
    if path == prefix:
        return cast(str, _payload(content))
    match = cast(re.Match[str], _CONTENT_BLOCK.fullmatch(path[len(prefix) :]))
    blocks = _array(content)
    block_fields = cast(dict[str, object], _fields(blocks[int(match[1])]))
    return cast(str, _payload(block_fields["text"]))


def _text_pair(left: Segment, right: Segment, path: str) -> tuple[str, str]:
    return _text_value(left, path), _text_value(right, path)


def _valid_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _masked_tokens(
    text: str, pattern: re.Pattern[str], *, timestamps: bool = False
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pieces: list[str] = []
    tokens: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        token = match.group()
        if timestamps and not _valid_timestamp(token):
            continue
        pieces.append(text[cursor : match.start()])
        tokens.append(token)
        cursor = match.end()
    pieces.append(text[cursor:])
    return tuple(pieces), tuple(tokens)


def _only_tokens_change(
    before: str, after: str, pattern: re.Pattern[str], *, timestamps: bool = False
) -> bool:
    before_pieces, before_tokens = _masked_tokens(
        before, pattern, timestamps=timestamps
    )
    after_pieces, after_tokens = _masked_tokens(after, pattern, timestamps=timestamps)
    return (
        bool(before_tokens)
        and len(before_tokens) == len(after_tokens)
        and before_pieces == after_pieces
        and before_tokens != after_tokens
    )


def _tool_order_changes(reference: CanonicalRequest, request: CanonicalRequest) -> bool:
    before = tuple(
        segment.value for segment in reference.segments if segment.kind == "tool"
    )
    after = tuple(
        segment.value for segment in request.segments if segment.kind == "tool"
    )
    return len(before) > 1 and before != after and Counter(before) == Counter(after)


def _stable_suffix(
    before: tuple[Segment, ...], after: tuple[Segment, ...], first: int
) -> bool:
    if len(before) != len(after):
        return False
    matching = 0
    for left, right in zip(  # pragma: no branch
        reversed(before), reversed(after), strict=True
    ):
        if left != right:
            break
        matching += 1
    return matching >= 2 and first < len(before) - matching


def _pair_codes(
    reference: CanonicalRequest, request: CanonicalRequest
) -> set[tuple[DiagnosisCode, str]]:
    pair = _pair_difference(reference.segments, request.segments)
    if reference.coverage_warnings or request.coverage_warnings:
        path = pair[1].path if pair is not None else "request"
        return {("coverage_limited", path)}
    codes: set[tuple[DiagnosisCode, str]] = set()
    if reference.context != request.context:
        codes.add(("context_changed", "context"))
    if pair is None:
        return codes
    index, divergence = pair
    path = divergence.path
    if path.startswith("tools") and _tool_order_changes(reference, request):
        codes.add(("tool_order_churn", path))
    if divergence.kind == "text_changed":
        before, after = _text_pair(
            reference.segments[index], request.segments[index], path
        )
        if _only_tokens_change(before, after, _TIMESTAMP, timestamps=True):
            codes.add(("volatile_timestamp", path))
        if _only_tokens_change(before, after, _IDENTIFIER):
            codes.add(("volatile_identifier", path))
        if before != after and _WHITESPACE.sub(" ", before).strip() == (
            _WHITESPACE.sub(" ", after).strip()
        ):
            codes.add(("whitespace_drift", path))
    if divergence.kind == "text_changed" and _stable_suffix(
        reference.segments, request.segments, index
    ):
        codes.add(("stable_suffix_after_drift", path))
    if not any(code not in ("context_changed",) for code, _ in codes):
        codes.add(("unclassified_drift", path))
    return codes


def _finding(code: DiagnosisCode, path: str, count: int) -> DiagnosisFinding:
    evidence, explanation, action = _INFO[code]
    return DiagnosisFinding(code, evidence, path, count, explanation, action)


def diagnose_requests(
    requests: Sequence[CanonicalRequest], *, limits: DiagnosisLimits | None = None
) -> DiagnosisResult:
    """Classify supported candidate causes without retaining request content.

    Each request after the first is compared with the first request in its cohort.
    Findings are evidence about this local model, not provider cache decisions.
    """
    selected_limits = limits if limits is not None else DiagnosisLimits()
    if not isinstance(selected_limits, DiagnosisLimits):
        raise InvalidRequestError("expected diagnosis limits")
    if not requests:
        raise InvalidRequestError("at least one canonical request is required")
    if len(requests) > selected_limits.max_requests:
        raise InvalidRequestError("diagnosis request limit exceeded")
    group = tuple(requests)
    if any(not isinstance(request, CanonicalRequest) for request in group):
        raise InvalidRequestError("expected canonical requests")
    total_segments = total_nodes = total_chars = total_work = 0
    reference_cost = 0
    for index, request in enumerate(group):
        total_segments += len(request.segments)
        if total_segments > selected_limits.max_segments:
            raise InvalidRequestError("diagnosis segment limit exceeded")
        nodes, chars = _measure_request(
            request,
            max_nodes=selected_limits.max_nodes - total_nodes,
            max_chars=selected_limits.max_text_chars - total_chars,
        )
        total_nodes += nodes
        total_chars += chars
        cost = nodes + chars
        if index == 0:
            reference_cost = cost
            if reference_cost * (len(group) - 1) > selected_limits.max_work_units:
                raise InvalidRequestError("diagnosis work limit exceeded")
        else:
            total_work += cost
            if (
                total_work + reference_cost * (len(group) - 1)
                > selected_limits.max_work_units
            ):
                raise InvalidRequestError("diagnosis work limit exceeded")
    total_work += reference_cost * (len(group) - 1)
    compare_requests(group)
    counts: Counter[tuple[DiagnosisCode, str]] = Counter()
    if len(group) == 1:
        counts[("insufficient_data", "request")] = 1
    else:
        for request in group[1:]:
            counts.update(_pair_codes(group[0], request))
    findings = tuple(
        _finding(code, path, count)
        for (code, path), count in sorted(
            counts.items(),
            key=lambda item: (
                -item[1],
                _CODE_ORDER.index(item[0][0]),
                item[0][1],
            ),
        )
    )
    return DiagnosisResult(group[0].provider, len(group), len(group) - 1, findings)
