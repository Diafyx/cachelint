"""Versioned, content-free corpus reports for terminals and automation."""

from __future__ import annotations

import json

from cachelint._compare import ComparisonResult, Divergence
from cachelint._corpus import CorpusResult
from cachelint._estimate import PrefixEstimate

CORPUS_REPORT_SCHEMA_VERSION = 1


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


def corpus_report(result: CorpusResult) -> dict[str, object]:
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


def render_corpus_json(result: CorpusResult) -> str:
    """Render deterministic UTF-8 compatible JSON with a final newline."""
    return json.dumps(corpus_report(result), indent=2, sort_keys=True) + "\n"


def render_corpus_text(result: CorpusResult) -> str:
    """Render a concise, fixed-label terminal report without source values."""
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
