"""Content-free explanations that keep modeled prefixes and usage distinct."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cachelint._compare import _pair_difference
from cachelint._corpus import (
    CorpusLimits,
    CorpusResult,
    _analyze_corpus_with_requests,
)
from cachelint._estimate import EstimateMethod, estimate_prefix
from cachelint._ingest import (
    CanonicalRequest,
    InvalidRequestError,
    Provider,
    SourceLocation,
)
from cachelint._observed import (
    UsageLimits,
    UsageObservation,
    UsageTrace,
    _analyze_usage_with_requests,
    _file,
)

ObservedFindingCode = Literal[
    "matching_prefix_no_read",
    "divergent_prefix_no_read",
    "read_below_modeled_minimum",
    "read_with_divergence",
    "context_changed_no_read",
    "short_modeled_prefix",
    "coverage_limited",
    "missing_observation",
]
EvidenceQuality = Literal["observed", "limited", "unknown"]
_STATUS_ORDER = (
    "reported",
    "absent",
    "invalid",
    "too_large",
    "streaming",
    "http_error",
    "incomplete",
    "unmatched",
    "missing_sidecar",
    "missing_trace",
)
_PRIORITY: tuple[ObservedFindingCode, ...] = (
    "matching_prefix_no_read",
    "divergent_prefix_no_read",
    "read_below_modeled_minimum",
    "read_with_divergence",
    "context_changed_no_read",
    "short_modeled_prefix",
    "coverage_limited",
    "missing_observation",
)
_EXPLANATIONS: dict[
    ObservedFindingCode, tuple[EvidenceQuality, str, tuple[str, ...], tuple[str, ...]]
] = {
    "matching_prefix_no_read": (
        "observed",
        "Modeled prompts match, but the provider reports zero cached read tokens.",
        (
            "The entry may have expired or request context may differ.",
            "Provider matching rules or eligibility may differ from this model.",
        ),
        ("Inspect context, coverage, cache controls, and call order.",),
    ),
    "divergent_prefix_no_read": (
        "observed",
        "The modeled prompt diverges and the provider reports zero cached reads.",
        ("An earlier prompt change may have reduced the reusable prefix.",),
        ("Inspect the safe path and place useful stable content earlier.",),
    ),
    "read_below_modeled_minimum": (
        "limited",
        "The provider reports a read despite a modeled prefix below the minimum.",
        (
            "The estimate omits provider framing and may undercount the prefix.",
            "The bundled model rule may not describe the current provider behavior.",
        ),
        ("Check usage fields and current model threshold documentation.",),
    ),
    "read_with_divergence": (
        "observed",
        "The provider reports a cache read despite modeled prompt divergence.",
        ("A shared prefix before the divergence may still have been reused.",),
        ("Inspect where the divergence occurs before changing request layout.",),
    ),
    "context_changed_no_read": (
        "limited",
        "The provider reports zero cached reads and request context differs.",
        ("A cache-affecting option or unmodeled context may differ.",),
        ("Review the fixed context categories for this request pair.",),
    ),
    "short_modeled_prefix": (
        "limited",
        "The modeled shared prefix is below the bundled model minimum.",
        ("The reusable prefix may be too short for the documented threshold.",),
        ("Check the model rule and place useful stable content earlier.",),
    ),
    "coverage_limited": (
        "limited",
        "Some content or context falls outside the local comparison model.",
        ("The modeled prefix may omit provider-relevant request parts.",),
        ("Review coverage warnings before attributing a usage result.",),
    ),
    "missing_observation": (
        "unknown",
        "No usable provider cache-read counter is available for a later request.",
        ("The sidecar, response usage, or complete response may be missing.",),
        ("Review the usage status and recording setup; do not count this as a miss.",),
    ),
}


@dataclass(frozen=True)
class CorpusUsageTrace:
    """Private request/sidecar pair and its pinned recorder provider."""

    provider: Provider
    requests: str | Path
    sidecar: str | Path


@dataclass(frozen=True)
class CorpusUsageGroup:
    """Content-free observed usage for one existing corpus cohort."""

    group_index: int
    provider: Provider
    request_count: int
    traced_request_count: int
    status_counts: tuple[tuple[str, int], ...]
    cache_read_positive_count: int
    cache_read_zero_count: int
    cache_read_unknown_count: int
    known_cache_read_tokens: int
    cache_write_positive_count: int
    cache_write_zero_count: int
    cache_write_unknown_count: int
    known_cache_write_tokens: int


@dataclass(frozen=True)
class ObservedFinding:
    """Fixed, evidence-labeled explanation with only a safe modeled path."""

    rank: int
    group_index: int
    provider: Provider
    code: ObservedFindingCode
    count: int
    path: str | None
    evidence_quality: EvidenceQuality
    observation: str
    plausible_causes: tuple[str, ...]
    actions: tuple[str, ...]


@dataclass(frozen=True)
class ObservedCorpusResult:
    """Request-only corpus plus opt-in usage summaries and ranked findings."""

    corpus: CorpusResult
    trace_count: int
    sidecar_row_count: int
    unassigned_attempt_count: int
    traced_request_count: int
    usage_groups: tuple[CorpusUsageGroup, ...]
    findings: tuple[ObservedFinding, ...]


def _counts(values: list[int | None]) -> tuple[int, int, int, int]:
    known = [value for value in values if value is not None]
    return (
        sum(value > 0 for value in known),
        sum(value == 0 for value in known),
        len(values) - len(known),
        sum(known),
    )


def _summarize_group(
    index: int,
    requests: list[CanonicalRequest],
    evidence: dict[int, UsageObservation],
) -> CorpusUsageGroup:
    observations = [evidence.get(id(request)) for request in requests]
    statuses = tuple(
        (
            status,
            sum(
                (item.usage_status if item is not None else "missing_trace") == status
                for item in observations
            ),
        )
        for status in _STATUS_ORDER
    )
    read = _counts(
        [item.cache_read_tokens if item is not None else None for item in observations]
    )
    write = _counts(
        [item.cache_write_tokens if item is not None else None for item in observations]
    )
    return CorpusUsageGroup(
        index,
        requests[0].provider,
        len(requests),
        sum(item is not None for item in observations),
        statuses,
        *read,
        *write,
    )


def _group_findings(
    index: int,
    requests: list[CanonicalRequest],
    evidence: dict[int, UsageObservation],
    estimate_method: EstimateMethod,
    has_model_minimum: bool,
) -> dict[tuple[int, ObservedFindingCode, str | None], int]:
    reference = requests[0]
    counts: dict[tuple[int, ObservedFindingCode, str | None], int] = {}

    def add(code: ObservedFindingCode, path: str | None = None) -> None:
        key = (index, code, path)
        counts[key] = counts.get(key, 0) + 1

    for request in requests[1:]:
        observation = evidence.get(id(request))
        read = observation.cache_read_tokens if observation is not None else None
        difference = _pair_difference(reference.segments, request.segments)
        path = difference[1].path if difference is not None else None
        if read is None:
            add("missing_observation")
        elif read == 0:
            if path is None:
                add("matching_prefix_no_read")
            else:
                add("divergent_prefix_no_read", path)
        elif path is not None:
            add("read_with_divergence", path)
        if read == 0 and reference.context != request.context:
            add("context_changed_no_read")
        if reference.coverage_warnings or request.coverage_warnings:
            add("coverage_limited")
        if has_model_minimum:
            common = (
                difference[0] if difference is not None else len(reference.segments)
            )
            partial = (
                (difference[1].path, difference[1].offset)
                if difference is not None and difference[1].offset is not None
                else None
            )
            estimate = estimate_prefix(reference, common, partial, estimate_method)
            if estimate.minimum_comparison == "estimate_below":
                add("short_modeled_prefix")
                if read is not None and read > 0:
                    add("read_below_modeled_minimum")
    return counts


def _rank_findings(
    counts: dict[tuple[int, ObservedFindingCode, str | None], int],
    providers: dict[int, Provider],
) -> tuple[ObservedFinding, ...]:
    priority = {code: index for index, code in enumerate(_PRIORITY)}
    ranked = sorted(
        counts.items(),
        key=lambda item: (
            priority[item[0][1]],
            -item[1],
            item[0][0],
            item[0][2] or "",
        ),
    )
    return tuple(
        ObservedFinding(
            rank,
            group_index,
            providers[group_index],
            code,
            count,
            path,
            *_EXPLANATIONS[code],
        )
        for rank, ((group_index, code, path), count) in enumerate(ranked, 1)
    )


def analyze_observed_corpus(
    paths: str | Path | Iterable[str | Path],
    *,
    usage_traces: Iterable[CorpusUsageTrace],
    provider: Provider | None = None,
    estimate_method: EstimateMethod = "heuristic",
    limits: CorpusLimits | None = None,
    usage_limits: UsageLimits | None = None,
) -> ObservedCorpusResult:
    """Join opt-in sidecars to a corpus and explain only supported evidence."""
    corpus, files = _analyze_corpus_with_requests(
        paths, provider=provider, estimate_method=estimate_method, limits=limits
    )
    traces = tuple(usage_traces)
    if not traces:
        raise InvalidRequestError("at least one corpus usage trace is required")
    if usage_limits is None:
        usage_limits = UsageLimits()
    if not isinstance(usage_limits, UsageLimits):
        raise InvalidRequestError("invalid usage limits")
    if len(traces) > usage_limits.max_traces:
        raise InvalidRequestError("usage trace limit exceeded")
    by_provider: dict[Provider, list[UsageTrace]] = {
        "anthropic": [],
        "openai": [],
    }
    traced_paths: set[Path] = set()
    for trace in traces:
        if not isinstance(trace, CorpusUsageTrace):
            raise InvalidRequestError(
                "corpus usage traces must be CorpusUsageTrace values"
            )
        if trace.provider not in ("anthropic", "openai"):
            raise InvalidRequestError("unsupported corpus usage provider")
        request_path = _file(trace.requests)
        if request_path not in files:
            raise InvalidRequestError(
                "usage request file is outside the corpus",
                SourceLocation(str(request_path)),
            )
        if request_path in traced_paths:
            raise InvalidRequestError(
                "duplicate corpus usage request file", SourceLocation(str(request_path))
            )
        traced_paths.add(request_path)
        by_provider[trace.provider].append(UsageTrace(request_path, trace.sidecar))
    evidence: dict[int, UsageObservation] = {}
    sidecar_rows = unassigned = usage_bytes = usage_requests = 0
    for selected_provider, provider_traces in by_provider.items():
        if not provider_traces:
            continue
        remaining_bytes = usage_limits.max_bytes - usage_bytes
        remaining_requests = usage_limits.max_requests - usage_requests
        remaining_rows = usage_limits.max_rows - sidecar_rows
        if min(remaining_bytes, remaining_requests, remaining_rows) <= 0:
            raise InvalidRequestError("combined corpus usage limit exceeded")
        usage, private_requests = _analyze_usage_with_requests(
            provider_traces,
            provider=selected_provider,
            limits=UsageLimits(
                max_traces=usage_limits.max_traces,
                max_bytes=remaining_bytes,
                max_requests=remaining_requests,
                max_rows=remaining_rows,
            ),
        )
        usage_bytes += usage.byte_count
        usage_requests += usage.request_count
        sidecar_rows += usage.sidecar_row_count
        unassigned += usage.unassigned_attempt_count
        positions: dict[Path, int] = {path: 0 for path in traced_paths}
        for (path, request), observation in zip(
            private_requests, usage.observations, strict=True
        ):
            if path not in files or positions[path] >= len(files[path]):
                raise InvalidRequestError("usage trace changed during corpus analysis")
            expected = files[path][positions[path]]
            if expected != request:
                raise InvalidRequestError("usage trace changed during corpus analysis")
            evidence[id(expected)] = observation
            positions[path] += 1
        for provider_trace in provider_traces:
            path = Path(provider_trace.requests)
            if positions[path] != len(files[path]):
                raise InvalidRequestError("usage trace changed during corpus analysis")
    grouped: dict[tuple[Provider, str], list[CanonicalRequest]] = {}
    for file_items in files.values():
        for request in file_items:
            grouped.setdefault((request.provider, request.model), []).append(request)
    usage_groups: list[CorpusUsageGroup] = []
    counts: dict[tuple[int, ObservedFindingCode, str | None], int] = {}
    providers: dict[int, Provider] = {}
    for index, key in enumerate(sorted(grouped), 1):
        group_requests = grouped[key]
        providers[index] = key[0]
        usage_groups.append(_summarize_group(index, group_requests, evidence))
        group_estimate = corpus.groups[index - 1].comparison.prefix_estimate
        has_minimum = (
            group_estimate is not None
            and group_estimate.minimum_cacheable_tokens is not None
        )
        counts.update(
            _group_findings(
                index, group_requests, evidence, estimate_method, has_minimum
            )
        )
    return ObservedCorpusResult(
        corpus,
        len(traces),
        sidecar_rows,
        unassigned,
        len(evidence),
        tuple(usage_groups),
        _rank_findings(counts, providers),
    )
