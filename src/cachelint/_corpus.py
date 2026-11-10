"""Bounded, deterministic analysis of private JSONL request corpora."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from cachelint._compare import (
    ComparisonResult,
    DivergenceKind,
    _pair_difference,
    compare_requests,
)
from cachelint._estimate import EstimateMethod, PrefixEstimate, estimate_prefix
from cachelint._ingest import (
    CanonicalRequest,
    InvalidRequestError,
    Provider,
    SourceLocation,
    _canonicalize_with_source,
    _parse_request,
)


@dataclass(frozen=True)
class CorpusLimits:
    """Hard bounds for one in-memory corpus analysis."""

    max_files: int = 128
    max_bytes: int = 16 * 1024 * 1024
    max_requests: int = 20_000
    max_entries: int = 4_096

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_files,
                self.max_bytes,
                self.max_requests,
                self.max_entries,
            )
        ):
            raise InvalidRequestError("corpus limits must be positive integers")


@dataclass(frozen=True)
class CorpusGroup:
    """One provider/model cohort; the model value is deliberately omitted."""

    index: int
    provider: Provider
    file_count: int
    request_count: int
    comparison: ComparisonResult
    paired_count: int
    identical_count: int
    diverged_count: int
    context_changed_count: int
    coverage_limited_count: int


@dataclass(frozen=True)
class CorpusPattern:
    """Observed divergence relative to a cohort's first request."""

    rank: int
    group_index: int
    provider: Provider
    category: str
    path: str
    kind: DivergenceKind
    common_prefix_segments: int
    count: int
    text_offset_min: int | None
    text_offset_max: int | None
    context_changed_count: int
    coverage_limited_count: int
    prefix_estimate: PrefixEstimate


@dataclass(frozen=True)
class CorpusResult:
    """Content-free results for all validated corpus files."""

    file_count: int
    byte_count: int
    request_count: int
    groups: tuple[CorpusGroup, ...]
    patterns: tuple[CorpusPattern, ...]
    provider_reported_cache_status: Literal["unavailable"] = "unavailable"


@dataclass
class _PatternCount:
    count: int = 0
    text_offset_min: int | None = None
    text_offset_max: int | None = None
    context_changed_count: int = 0
    coverage_limited_count: int = 0


def _cohort_patterns(
    requests: list[CanonicalRequest],
    group_index: int,
    estimate_method: EstimateMethod,
) -> tuple[tuple[CorpusPattern, ...], tuple[int, int, int, int]]:
    """Pair each later observation with one fixed reference in linear passes."""
    reference = requests[0]
    counts: dict[tuple[int, str, DivergenceKind], _PatternCount] = {}
    identical = diverged = context_changed = coverage_limited = 0
    for request in requests[1:]:
        context_differs = request.context != reference.context
        coverage_is_limited = bool(
            request.coverage_warnings or reference.coverage_warnings
        )
        context_changed += context_differs
        coverage_limited += coverage_is_limited
        difference = _pair_difference(reference.segments, request.segments)
        if difference is None:
            identical += 1
            continue
        diverged += 1
        common, divergence = difference
        key = (common, divergence.path, divergence.kind)
        aggregate = counts.setdefault(key, _PatternCount())
        aggregate.count += 1
        aggregate.context_changed_count += context_differs
        aggregate.coverage_limited_count += coverage_is_limited
        if divergence.offset is not None:
            aggregate.text_offset_min = (
                min(aggregate.text_offset_min, divergence.offset)
                if aggregate.text_offset_min is not None
                else divergence.offset
            )
            aggregate.text_offset_max = (
                max(aggregate.text_offset_max, divergence.offset)
                if aggregate.text_offset_max is not None
                else divergence.offset
            )
    patterns = tuple(
        CorpusPattern(
            rank=0,
            group_index=group_index,
            provider=reference.provider,
            category=path.split("[", 1)[0].split(".", 1)[0],
            path=path,
            kind=kind,
            common_prefix_segments=common,
            count=aggregate.count,
            text_offset_min=aggregate.text_offset_min,
            text_offset_max=aggregate.text_offset_max,
            context_changed_count=aggregate.context_changed_count,
            coverage_limited_count=aggregate.coverage_limited_count,
            prefix_estimate=estimate_prefix(
                reference,
                common,
                (path, aggregate.text_offset_min)
                if aggregate.text_offset_min is not None
                else None,
                estimate_method,
            ),
        )
        for (common, path, kind), aggregate in counts.items()
    )
    return patterns, (identical, diverged, context_changed, coverage_limited)


def _reject_symlink_components(path: Path) -> None:
    for component in (path, *path.parents):
        try:
            if component.is_symlink():
                raise InvalidRequestError(
                    "symlink input is not supported", SourceLocation(str(path))
                )
        except OSError:
            raise InvalidRequestError(
                "unreadable input path", SourceLocation(str(path))
            ) from None


def _discover_files(
    inputs: Iterable[str | Path], limits: CorpusLimits
) -> tuple[Path, ...]:
    files: dict[str, Path] = {}
    entries_seen = 0
    roots_seen = 0

    def add_file(path: Path) -> None:
        _reject_symlink_components(path)
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
        except (OSError, RuntimeError):
            raise InvalidRequestError(
                "unreadable input file", SourceLocation(str(path))
            ) from None
        if not stat.S_ISREG(metadata.st_mode):
            raise InvalidRequestError(
                "input must be a regular file", SourceLocation(str(path))
            )
        key = os.path.normcase(str(resolved))
        if key in files:
            raise InvalidRequestError("duplicate input file", SourceLocation(str(path)))
        if len(files) >= limits.max_files:
            raise InvalidRequestError("corpus file limit exceeded")
        files[key] = resolved

    for root in inputs:
        roots_seen += 1
        entries_seen += 1
        if entries_seen > limits.max_entries:
            raise InvalidRequestError("corpus discovery limit exceeded")
        if not isinstance(root, str | Path) or (isinstance(root, str) and not root):
            raise InvalidRequestError("corpus paths must be nonempty strings or Paths")
        path = Path(root)
        _reject_symlink_components(path)
        try:
            mode = path.stat().st_mode
        except OSError:
            raise InvalidRequestError(
                "unreadable input path", SourceLocation(str(path))
            ) from None
        if stat.S_ISREG(mode):
            add_file(path)
            continue
        if not stat.S_ISDIR(mode):
            raise InvalidRequestError(
                "input must be a regular file or directory",
                SourceLocation(str(path)),
            )
        stack = [path]
        while stack:
            directory = stack.pop()
            children: list[Path] = []
            try:
                with os.scandir(directory) as listing:
                    for entry in listing:
                        entries_seen += 1
                        if entries_seen > limits.max_entries:
                            raise InvalidRequestError("corpus discovery limit exceeded")
                        child = Path(entry.path)
                        if entry.is_symlink():
                            raise InvalidRequestError(
                                "symlink input is not supported",
                                SourceLocation(str(child)),
                            )
                        if entry.is_dir(follow_symlinks=False):
                            children.append(child)
                        elif entry.is_file(follow_symlinks=False):
                            if child.suffix.lower() == ".jsonl":
                                add_file(child)
                        elif child.suffix.lower() == ".jsonl":
                            raise InvalidRequestError(
                                "input must be a regular file",
                                SourceLocation(str(child)),
                            )
            except OSError:
                raise InvalidRequestError(
                    "unreadable input directory", SourceLocation(str(directory))
                ) from None
            stack.extend(sorted(children, key=lambda item: item.name, reverse=True))
    if roots_seen == 0:
        raise InvalidRequestError("at least one corpus path is required")
    if not files:
        raise InvalidRequestError("no JSONL input files found")
    return tuple(files[key] for key in sorted(files))


def analyze_corpus(
    paths: str | Path | Iterable[str | Path],
    *,
    provider: Provider | None = None,
    estimate_method: EstimateMethod = "heuristic",
    limits: CorpusLimits | None = None,
) -> CorpusResult:
    """Group bounded JSONL inputs by provider/model and compare each group.

    Files are read in normalized absolute-path order, then physical line order.
    Results omit model strings, source paths, and all request values.
    """
    if provider not in (None, "anthropic", "openai"):
        raise InvalidRequestError("unsupported provider")
    if estimate_method not in ("heuristic", "tiktoken"):
        raise InvalidRequestError("unsupported estimate method")
    if limits is None:
        limits = CorpusLimits()
    if not isinstance(limits, CorpusLimits):
        raise InvalidRequestError("invalid corpus limits")
    if isinstance(paths, str | Path):
        paths = (paths,)
    files = _discover_files(paths, limits)
    groups: dict[tuple[Provider, str], list[CanonicalRequest]] = {}
    group_files: dict[tuple[Provider, str], set[int]] = {}
    byte_count = 0
    request_count = 0
    for file_index, path in enumerate(files):
        source_path = str(path)
        try:
            with path.open("rb") as stream:
                line_number = 0
                while True:
                    remaining = limits.max_bytes - byte_count
                    raw_line = stream.readline(remaining + 1)
                    if not raw_line:
                        break
                    if len(raw_line) > remaining:
                        raise InvalidRequestError(
                            "corpus byte limit exceeded", SourceLocation(source_path)
                        )
                    byte_count += len(raw_line)
                    line_number += 1
                    source = SourceLocation(source_path, line_number)
                    try:
                        line = raw_line.decode(
                            "utf-8-sig" if line_number == 1 else "utf-8"
                        )
                    except UnicodeError:
                        raise InvalidRequestError("invalid UTF-8", source) from None
                    if not line.strip():
                        continue
                    if request_count >= limits.max_requests:
                        raise InvalidRequestError(
                            "corpus request limit exceeded", source
                        )
                    request = _canonicalize_with_source(
                        _parse_request(line, source), provider, source
                    )
                    key = (request.provider, request.model)
                    groups.setdefault(key, []).append(request)
                    group_files.setdefault(key, set()).add(file_index)
                    request_count += 1
        except OSError:
            raise InvalidRequestError(
                "unreadable input file", SourceLocation(source_path)
            ) from None
    summaries: list[CorpusGroup] = []
    patterns: list[CorpusPattern] = []
    for index, key in enumerate(sorted(groups), 1):
        requests = groups[key]
        cohort_patterns, counts = _cohort_patterns(requests, index, estimate_method)
        identical, diverged, context_changed, coverage_limited = counts
        patterns.extend(cohort_patterns)
        summaries.append(
            CorpusGroup(
                index=index,
                provider=key[0],
                file_count=len(group_files[key]),
                request_count=len(requests),
                comparison=compare_requests(requests, estimate_method=estimate_method),
                paired_count=len(requests) - 1,
                identical_count=identical,
                diverged_count=diverged,
                context_changed_count=context_changed,
                coverage_limited_count=coverage_limited,
            )
        )
    ranked = tuple(
        replace(pattern, rank=rank)
        for rank, pattern in enumerate(
            sorted(
                patterns,
                key=lambda item: (
                    -item.count,
                    item.common_prefix_segments,
                    item.group_index,
                    item.path,
                    item.kind,
                ),
            ),
            1,
        )
    )
    return CorpusResult(len(files), byte_count, request_count, tuple(summaries), ranked)
