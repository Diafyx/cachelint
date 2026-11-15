"""Bounded corpus diagnosis and versioned, content-free reports."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cachelint._corpus import CorpusLimits, _analyze_corpus_with_requests
from cachelint._ingest import CanonicalRequest, InvalidRequestError, Provider
from cachelint.baseline import _validate
from cachelint.diagnosis import (
    DiagnosisFinding,
    DiagnosisLimits,
    DiagnosisResult,
    _finding,
    diagnose_requests,
)

DIAGNOSIS_REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DiagnosisCorpusGroup:
    """One safe provider/model cohort, with the model name omitted."""

    index: int
    diagnosis: DiagnosisResult


@dataclass(frozen=True)
class RankedDiagnosisFinding:
    """A finding ranked across all cohorts by frequency."""

    rank: int
    group_index: int
    provider: Provider
    finding: DiagnosisFinding


@dataclass(frozen=True)
class DiagnosisCorpusResult:
    """Content-free results from bounded private JSONL inputs."""

    file_count: int
    byte_count: int
    request_count: int
    groups: tuple[DiagnosisCorpusGroup, ...]
    findings: tuple[RankedDiagnosisFinding, ...]


def analyze_diagnosis(
    paths: str | Path | Iterable[str | Path],
    *,
    provider: Provider | None = None,
    corpus_limits: CorpusLimits | None = None,
    diagnosis_limits: DiagnosisLimits | None = None,
) -> DiagnosisCorpusResult:
    """Diagnose each provider/model cohort from bounded JSONL files or directories.

    Files are read in normalized absolute-path order, then physical line order.
    Public results omit model names, paths to source files, and request values.
    """
    selected_limits = diagnosis_limits or DiagnosisLimits()
    if not isinstance(selected_limits, DiagnosisLimits):
        raise InvalidRequestError("expected diagnosis limits")
    corpus, file_requests = _analyze_corpus_with_requests(
        paths, provider=provider, limits=corpus_limits
    )
    cohorts: dict[tuple[Provider, str], list[CanonicalRequest]] = {}
    for requests in file_requests.values():
        for request in requests:
            cohorts.setdefault((request.provider, request.model), []).append(request)
    groups = tuple(
        DiagnosisCorpusGroup(
            index, diagnose_requests(cohorts[key], limits=selected_limits)
        )
        for index, key in enumerate(sorted(cohorts), 1)
    )
    unranked = [
        (group.index, group.diagnosis.provider, local_rank, finding)
        for group in groups
        for local_rank, finding in enumerate(group.diagnosis.findings)
    ]
    unranked.sort(key=lambda item: (-item[3].count, item[0], item[2]))
    findings = tuple(
        RankedDiagnosisFinding(rank, group_index, provider_name, finding)
        for rank, (group_index, provider_name, _, finding) in enumerate(unranked, 1)
    )
    return DiagnosisCorpusResult(
        corpus.file_count, corpus.byte_count, corpus.request_count, groups, findings
    )


def diagnosis_report(result: DiagnosisCorpusResult) -> dict[str, Any]:
    """Project and validate the separately versioned safe report contract."""
    document: dict[str, Any] = {
        "schema_version": DIAGNOSIS_REPORT_SCHEMA_VERSION,
        "report_type": "cachelint.diagnosis",
        "summary": {
            "file_count": result.file_count,
            "byte_count": result.byte_count,
            "request_count": result.request_count,
            "cohort_count": len(result.groups),
            "finding_count": len(result.findings),
            "provider_reported_cache_status": "unavailable",
        },
        "groups": [
            {
                "index": group.index,
                "provider": group.diagnosis.provider,
                "request_count": group.diagnosis.request_count,
                "paired_count": group.diagnosis.paired_count,
                "finding_count": len(group.diagnosis.findings),
            }
            for group in result.groups
        ],
        "findings": [
            {
                "rank": item.rank,
                "group_index": item.group_index,
                "provider": item.provider,
                "code": item.finding.code,
                "evidence": item.finding.evidence,
                "path": item.finding.path,
                "count": item.finding.count,
                "explanation": item.finding.explanation,
                "action": item.finding.action,
            }
            for item in result.findings
        ],
    }
    _validate(document, "diagnosis_report_v1.schema.json")
    if [group.index for group in result.groups] != list(
        range(1, len(result.groups) + 1)
    ):
        raise InvalidRequestError("noncontiguous diagnosis group indexes")
    if (
        sum(group.diagnosis.request_count for group in result.groups)
        != result.request_count
    ):
        raise InvalidRequestError("inconsistent diagnosis request count")
    if [item.rank for item in result.findings] != list(
        range(1, len(result.findings) + 1)
    ):
        raise InvalidRequestError("noncontiguous diagnosis ranks")
    for item in result.findings:
        if (
            item.group_index > len(result.groups)
            or item.provider != result.groups[item.group_index - 1].diagnosis.provider
        ):
            raise InvalidRequestError("inconsistent diagnosis cohort")
        expected = _finding(item.finding.code, item.finding.path, item.finding.count)
        if item.finding != expected:
            raise InvalidRequestError("unsupported diagnosis wording")
    return document


def render_diagnosis_json(result: DiagnosisCorpusResult) -> str:
    """Render validated, deterministic JSON with one final newline."""
    return json.dumps(diagnosis_report(result), indent=2, sort_keys=True) + "\n"


def render_diagnosis_text(result: DiagnosisCorpusResult) -> str:
    """Render ranked causes without reading source values or paths."""
    diagnosis_report(result)
    lines = [
        "Diagnosis report",
        f"Files: {result.file_count}",
        f"Bytes: {result.byte_count}",
        f"Requests: {result.request_count}",
        f"Cohorts: {len(result.groups)}",
        f"Ranked findings: {len(result.findings)}",
        "Provider-reported cached usage: unavailable (request bodies only)",
    ]
    for group in result.groups:
        lines.append(
            f"Cohort {group.index}: {group.diagnosis.provider}; "
            f"requests {group.diagnosis.request_count}; "
            f"pairs {group.diagnosis.paired_count}"
        )
    if not result.groups:
        lines.append("No request cohorts found.")
    for item in result.findings:
        finding = item.finding
        lines.extend(
            [
                f"#{item.rank} cohort {item.group_index}: {finding.code} "
                f"({finding.evidence}); count {finding.count}; path {finding.path}",
                f"  Evidence: {finding.explanation}",
                f"  Action: {finding.action}",
            ]
        )
    return "\n".join(lines) + "\n"
