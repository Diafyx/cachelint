"""Versioned, content-free corpus reports for terminals and automation."""

from __future__ import annotations

import json

from cachelint._compare import ComparisonResult, Divergence
from cachelint._corpus import CorpusResult
from cachelint._estimate import PrefixEstimate
from cachelint._evidence import ObservedCorpusResult
from cachelint._observed import UsageResult

CORPUS_REPORT_SCHEMA_VERSION = 1
OBSERVED_CORPUS_REPORT_SCHEMA_VERSION = 2
USAGE_REPORT_SCHEMA_VERSION = 1


def _estimate_document(estimate: PrefixEstimate | None) -> dict[str, object] | None:
    if estimate is None:
        return None
    return {
        "tokens": estimate.tokens,
        "method": estimate.method,
        "uncertainty": estimate.uncertainty,
        "minimum_cacheable_tokens": estimate.minimum_cacheable_tokens,
        "minimum_comparison": estimate.minimum_comparison,
        "minimum_source": estimate.minimum_source,
        "rules_reviewed_on": estimate.rules_reviewed_on,
    }


def _divergence_document(divergence: Divergence | None) -> dict[str, object] | None:
    if divergence is None:
        return None
    return {
        "path": divergence.path,
        "kind": divergence.kind,
        "offset": divergence.offset,
    }


def _comparison_document(comparison: ComparisonResult) -> dict[str, object]:
    return {
        "prompt_status": comparison.prompt_status,
        "common_prefix_segments": comparison.common_prefix_segments,
        "first_divergence": _divergence_document(comparison.first_divergence),
        "context_differences": [
            item.category for item in comparison.context_differences
        ],
        "coverage_warnings": list(comparison.coverage_warnings),
        "finding_codes": [item.code for item in comparison.findings],
        "prefix_estimate": _estimate_document(comparison.prefix_estimate),
    }


def _corpus_report_v1(result: CorpusResult) -> dict[str, object]:
    """Project only approved fields from a corpus result into schema version 1."""
    return {
        "schema_version": CORPUS_REPORT_SCHEMA_VERSION,
        "report_type": "cachelint.corpus",
        "summary": {
            "file_count": result.file_count,
            "byte_count": result.byte_count,
            "request_count": result.request_count,
            "cohort_count": len(result.groups),
            "pattern_count": len(result.patterns),
            "provider_reported_cache_status": result.provider_reported_cache_status,
        },
        "groups": [
            {
                "index": group.index,
                "provider": group.provider,
                "file_count": group.file_count,
                "request_count": group.request_count,
                "paired_count": group.paired_count,
                "identical_count": group.identical_count,
                "diverged_count": group.diverged_count,
                "context_changed_count": group.context_changed_count,
                "coverage_limited_count": group.coverage_limited_count,
                "comparison": _comparison_document(group.comparison),
            }
            for group in result.groups
        ],
        "patterns": [
            {
                "rank": pattern.rank,
                "group_index": pattern.group_index,
                "provider": pattern.provider,
                "category": pattern.category,
                "path": pattern.path,
                "kind": pattern.kind,
                "common_prefix_segments": pattern.common_prefix_segments,
                "count": pattern.count,
                "text_offset_min": pattern.text_offset_min,
                "text_offset_max": pattern.text_offset_max,
                "context_changed_count": pattern.context_changed_count,
                "coverage_limited_count": pattern.coverage_limited_count,
                "prefix_estimate": _estimate_document(pattern.prefix_estimate),
            }
            for pattern in result.patterns
        ],
    }


def corpus_report(result: CorpusResult | ObservedCorpusResult) -> dict[str, object]:
    """Project request-only v1 or observed v2 without private input values."""
    if isinstance(result, CorpusResult):
        return _corpus_report_v1(result)
    document = _corpus_report_v1(result.corpus)
    document["schema_version"] = OBSERVED_CORPUS_REPORT_SCHEMA_VERSION
    summary = document["summary"]
    assert isinstance(summary, dict)
    summary.update(
        provider_reported_cache_status="sidecar",
        usage_trace_count=result.trace_count,
        sidecar_row_count=result.sidecar_row_count,
        unassigned_attempt_count=result.unassigned_attempt_count,
        traced_request_count=result.traced_request_count,
        observed_finding_count=len(result.findings),
    )
    groups = document["groups"]
    assert isinstance(groups, list)
    for group, usage in zip(groups, result.usage_groups, strict=True):
        assert isinstance(group, dict)
        group["usage"] = {
            "request_count": usage.request_count,
            "traced_request_count": usage.traced_request_count,
            "status_counts": dict(usage.status_counts),
            "cache_read_positive_count": usage.cache_read_positive_count,
            "cache_read_zero_count": usage.cache_read_zero_count,
            "cache_read_unknown_count": usage.cache_read_unknown_count,
            "known_cache_read_tokens": usage.known_cache_read_tokens,
            "cache_write_positive_count": usage.cache_write_positive_count,
            "cache_write_zero_count": usage.cache_write_zero_count,
            "cache_write_unknown_count": usage.cache_write_unknown_count,
            "known_cache_write_tokens": usage.known_cache_write_tokens,
        }
    document["observed_findings"] = [
        {
            "rank": finding.rank,
            "group_index": finding.group_index,
            "provider": finding.provider,
            "code": finding.code,
            "count": finding.count,
            "path": finding.path,
            "evidence_quality": finding.evidence_quality,
            "observation": finding.observation,
            "plausible_causes": list(finding.plausible_causes),
            "actions": list(finding.actions),
        }
        for finding in result.findings
    ]
    return document


def render_corpus_json(result: CorpusResult | ObservedCorpusResult) -> str:
    """Render deterministic UTF-8 compatible JSON with a final newline."""
    return json.dumps(corpus_report(result), indent=2, sort_keys=True) + "\n"


def render_corpus_text(result: CorpusResult | ObservedCorpusResult) -> str:
    """Render a concise, fixed-label terminal report without source values."""
    if isinstance(result, ObservedCorpusResult):
        lines = render_corpus_text(result.corpus).rstrip("\n").split("\n")
        lines[6] = "Provider-reported cached usage: opt-in sidecars"
        lines.append(
            f"Observed sidecars: {result.trace_count}; traced requests: "
            f"{result.traced_request_count}; rows: {result.sidecar_row_count}; "
            f"unassigned attempts: {result.unassigned_attempt_count}"
        )
        for usage_group in result.usage_groups:
            lines.append(
                f"Cohort {usage_group.group_index} observed: "
                f"read +{usage_group.cache_read_positive_count} "
                f"zero {usage_group.cache_read_zero_count} "
                f"unknown {usage_group.cache_read_unknown_count}; "
                f"known read tokens {usage_group.known_cache_read_tokens}; "
                f"write +{usage_group.cache_write_positive_count} "
                f"zero {usage_group.cache_write_zero_count} "
                f"unknown {usage_group.cache_write_unknown_count}"
            )
        for finding in result.findings:
            lines.append(
                f"Observed #{finding.rank} cohort {finding.group_index}: "
                f"{finding.code} ({finding.evidence_quality}); "
                f"count {finding.count}; path {finding.path or 'none'}"
            )
            lines.append(f"  Observation: {finding.observation}")
            lines.extend(f"  Possible cause: {x}" for x in finding.plausible_causes)
            lines.extend(f"  Action: {x}" for x in finding.actions)
        return "\n".join(lines) + "\n"
    lines = [
        "Corpus report",
        f"Files: {result.file_count}",
        f"Bytes: {result.byte_count}",
        f"Requests: {result.request_count}",
        f"Cohorts: {len(result.groups)}",
        f"Ranked patterns: {len(result.patterns)}",
        "Provider-reported cached usage: unavailable (request bodies only)",
    ]
    for group in result.groups:
        lines.extend(
            [
                f"Cohort {group.index}: {group.provider}",
                f"  Files: {group.file_count}",
                f"  Requests: {group.request_count}; pairs: {group.paired_count}",
                f"  Modeled prompt: {group.comparison.prompt_status}",
                f"  Identical pairs: {group.identical_count}",
                f"  Diverged pairs: {group.diverged_count}",
                f"  Context-changed pairs: {group.context_changed_count}",
                f"  Coverage-limited pairs: {group.coverage_limited_count}",
            ]
        )
    if not result.groups:
        lines.append("No request cohorts found.")
    for pattern in result.patterns:
        offset = (
            ""
            if pattern.text_offset_min is None
            else f"; text offsets: {pattern.text_offset_min}-{pattern.text_offset_max}"
        )
        lines.append(
            f"#{pattern.rank} cohort {pattern.group_index}: "
            f"{pattern.category} {pattern.path} "
            f"({pattern.kind}); count: {pattern.count}; complete prefix segments: "
            f"{pattern.common_prefix_segments}; ~{pattern.prefix_estimate.tokens} "
            f"modeled tokens ({pattern.prefix_estimate.method}, advisory){offset}"
        )
    return "\n".join(lines) + "\n"


def usage_report(result: UsageResult) -> dict[str, object]:
    """Project only reviewed observation fields into usage report version 1."""
    return {
        "schema_version": USAGE_REPORT_SCHEMA_VERSION,
        "report_type": "cachelint.usage",
        "summary": {
            "provider": result.provider,
            "trace_count": result.trace_count,
            "byte_count": result.byte_count,
            "request_count": result.request_count,
            "sidecar_row_count": result.sidecar_row_count,
            "unassigned_attempt_count": result.unassigned_attempt_count,
            "cohort_count": len(result.groups),
        },
        "groups": [
            {
                "index": group.index,
                "provider": group.provider,
                "request_count": group.request_count,
                "status_counts": dict(group.status_counts),
                "cache_read_positive_count": group.cache_read_positive_count,
                "cache_read_zero_count": group.cache_read_zero_count,
                "cache_read_unknown_count": group.cache_read_unknown_count,
                "known_cache_read_tokens": group.known_cache_read_tokens,
                "cache_write_positive_count": group.cache_write_positive_count,
                "cache_write_zero_count": group.cache_write_zero_count,
                "cache_write_unknown_count": group.cache_write_unknown_count,
                "known_cache_write_tokens": group.known_cache_write_tokens,
            }
            for group in result.groups
        ],
        "observations": [
            {
                "trace_index": item.trace_index,
                "request_index": item.request_index,
                "group_index": item.group_index,
                "attempt_id": item.attempt_id,
                "usage_status": item.usage_status,
                "cache_read_tokens": item.cache_read_tokens,
                "cache_write_tokens": item.cache_write_tokens,
            }
            for item in result.observations
        ],
    }


def render_usage_json(result: UsageResult) -> str:
    """Render deterministic, content-free JSON with one final newline."""
    return json.dumps(usage_report(result), indent=2, sort_keys=True) + "\n"


def render_usage_text(result: UsageResult) -> str:
    """Render observed counts without converting unknowns into misses."""
    lines = [
        "Usage observation report",
        f"Provider: {result.provider}",
        f"Traces: {result.trace_count}",
        f"Requests: {result.request_count}",
        f"Sidecar rows: {result.sidecar_row_count}",
        f"Unassigned attempts: {result.unassigned_attempt_count}",
        f"Cohorts: {len(result.groups)}",
    ]
    for group in result.groups:
        statuses = dict(group.status_counts)
        lines.extend(
            [
                f"Cohort {group.index}: {group.provider}",
                f"  Requests: {group.request_count}",
                (
                    f"  Usage reported: {statuses['reported']}; "
                    f"missing sidecar: {statuses['missing_sidecar']}"
                ),
                (
                    f"  Cache read: positive {group.cache_read_positive_count}; "
                    f"zero {group.cache_read_zero_count}; "
                    f"unknown {group.cache_read_unknown_count}"
                ),
                (
                    f"  Known cache-read tokens: {group.known_cache_read_tokens} "
                    "(partial when unknown > 0)"
                ),
                (
                    f"  Cache write: positive {group.cache_write_positive_count}; "
                    f"zero {group.cache_write_zero_count}; "
                    f"unknown {group.cache_write_unknown_count}"
                ),
                (
                    f"  Known cache-write tokens: {group.known_cache_write_tokens} "
                    "(partial when unknown > 0)"
                ),
            ]
        )
    if not result.groups:
        lines.append("No request cohorts found.")
    lines.append("Provider usage is an observation, not proof of a private cache key.")
    return "\n".join(lines) + "\n"
