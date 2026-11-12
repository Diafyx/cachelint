"""Strict, bounded joins of private request traces and usage sidecars."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from cachelint._corpus import _reject_symlink_components
from cachelint._ingest import (
    CanonicalRequest,
    InvalidRequestError,
    Provider,
    SourceLocation,
    _canonicalize_with_source,
    _parse_request,
)
from cachelint._usage import UsageStatus, _invalid_constant, _unique_object

EvidenceStatus = UsageStatus | Literal["missing_sidecar"]
_CAPTURE_STATUSES = frozenset({"written", "skipped", "failed", "unmatched"})
_USAGE_STATUSES = frozenset(
    {
        "reported",
        "absent",
        "invalid",
        "too_large",
        "streaming",
        "http_error",
        "incomplete",
        "unmatched",
    }
)
_ANTHROPIC_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)
_OPENAI_KEYS = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"}
)
_SKIP_CODES = frozenset(
    {"nonbuffered_body", "invalid_body", "credential_field", "size_limit"}
)
_ROW_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "capture_status",
        "capture_code",
        "request_offset",
        "usage_status",
        "usage",
    }
)
_EVIDENCE_STATUSES: tuple[EvidenceStatus, ...] = (
    "reported",
    "absent",
    "invalid",
    "too_large",
    "streaming",
    "http_error",
    "incomplete",
    "unmatched",
    "missing_sidecar",
)


@dataclass(frozen=True)
class UsageTrace:
    """One raw recording and its same-session sidecar (private input paths)."""

    requests: str | Path
    sidecar: str | Path


@dataclass(frozen=True)
class UsageLimits:
    """Hard bounds across all trace pairs in one observation analysis."""

    max_traces: int = 128
    max_bytes: int = 16 * 1024 * 1024
    max_requests: int = 20_000
    max_rows: int = 40_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_traces,
                self.max_bytes,
                self.max_requests,
                self.max_rows,
            )
        ):
            raise InvalidRequestError("usage limits must be positive integers")


@dataclass(frozen=True)
class UsageObservation:
    """Content-free evidence for one raw request in deterministic input order."""

    trace_index: int
    request_index: int
    group_index: int
    attempt_id: int | None
    usage_status: EvidenceStatus
    cache_read_tokens: int | None
    cache_write_tokens: int | None


@dataclass(frozen=True)
class UsageGroup:
    """One provider/model cohort, with the model deliberately omitted."""

    index: int
    provider: Provider
    request_count: int
    status_counts: tuple[tuple[EvidenceStatus, int], ...]
    cache_read_positive_count: int
    cache_read_zero_count: int
    cache_read_unknown_count: int
    known_cache_read_tokens: int
    cache_write_positive_count: int
    cache_write_zero_count: int
    cache_write_unknown_count: int
    known_cache_write_tokens: int


@dataclass(frozen=True)
class UsageResult:
    """Sanitized observations; no request, model, source path, or response text."""

    provider: Provider
    trace_count: int
    byte_count: int
    request_count: int
    sidecar_row_count: int
    unassigned_attempt_count: int
    groups: tuple[UsageGroup, ...]
    observations: tuple[UsageObservation, ...]


@dataclass(frozen=True)
class _SidecarRow:
    attempt_id: int
    capture_status: str
    request_offset: int | None
    usage_status: UsageStatus
    usage: dict[str, int]


def _file(path: str | Path) -> Path:
    if not isinstance(path, str | Path) or not str(path):
        raise InvalidRequestError("usage paths must be nonempty strings or Paths")
    selected = Path(path)
    _reject_symlink_components(selected)
    try:
        resolved = selected.resolve(strict=True)
        mode = resolved.stat().st_mode
    except (OSError, RuntimeError):
        raise InvalidRequestError(
            "unreadable usage input", SourceLocation(str(selected))
        ) from None
    if not stat.S_ISREG(mode):
        raise InvalidRequestError(
            "usage input must be a regular file", SourceLocation(str(selected))
        )
    return resolved


def _read_lines(path: Path, remaining: int) -> tuple[list[tuple[int, int, bytes]], int]:
    try:
        with path.open("rb") as stream:
            data = stream.read(remaining + 1)
    except OSError:
        raise InvalidRequestError(
            "unreadable usage input", SourceLocation(str(path))
        ) from None
    if len(data) > remaining:
        raise InvalidRequestError(
            "usage byte limit exceeded", SourceLocation(str(path))
        )
    lines: list[tuple[int, int, bytes]] = []
    offset = 0
    for number, line in enumerate(data.splitlines(keepends=True), 1):
        if not line.endswith(b"\n"):
            raise InvalidRequestError(
                "incomplete JSONL line", SourceLocation(str(path), number)
            )
        lines.append((offset, number, line))
        offset += len(line)
    return lines, len(data)


def _request_rows(
    path: Path, provider: Provider, remaining: int, max_requests: int
) -> tuple[dict[int, CanonicalRequest], int]:
    lines, size = _read_lines(path, remaining)
    requests: dict[int, CanonicalRequest] = {}
    for offset, number, raw in lines:
        if not raw.strip():
            continue
        source = SourceLocation(str(path), number)
        if len(requests) >= max_requests:
            raise InvalidRequestError("usage request limit exceeded", source)
        try:
            line = raw.decode("utf-8-sig" if number == 1 else "utf-8")
        except UnicodeError:
            raise InvalidRequestError("invalid UTF-8", source) from None
        request = _canonicalize_with_source(
            _parse_request(line, source), provider, source
        )
        requests[offset] = request
    return requests, size


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


def _parse_sidecar_row(
    raw: bytes, provider: Provider, source: SourceLocation
) -> _SidecarRow:
    try:
        row = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidRequestError("invalid usage sidecar JSON", source) from None
    if not isinstance(row, dict) or not set(row).issubset(_ROW_KEYS):
        raise InvalidRequestError("invalid usage sidecar row", source)
    if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
        raise InvalidRequestError("unsupported usage schema version", source)
    if not _positive(row.get("attempt_id")):
        raise InvalidRequestError("invalid usage attempt ID", source)
    capture_status = row.get("capture_status")
    usage_status = row.get("usage_status")
    if (
        not isinstance(capture_status, str)
        or capture_status not in _CAPTURE_STATUSES
        or not isinstance(usage_status, str)
        or usage_status not in _USAGE_STATUSES
    ):
        raise InvalidRequestError("invalid usage sidecar status", source)
    offset = row.get("request_offset")
    if capture_status == "written":
        if not _nonnegative(offset) or "capture_code" in row:
            raise InvalidRequestError("invalid written usage coordinate", source)
    elif "request_offset" in row:
        raise InvalidRequestError("unexpected usage request offset", source)
    code = row.get("capture_code")
    if capture_status == "skipped":
        if not isinstance(code, str) or code not in _SKIP_CODES:
            raise InvalidRequestError("invalid usage capture code", source)
    elif capture_status == "failed":
        if code != "write_failed":
            raise InvalidRequestError("invalid usage capture code", source)
    elif "capture_code" in row:
        raise InvalidRequestError("unexpected usage capture code", source)
    usage = row.get("usage")
    if usage_status == "reported":
        allowed = _ANTHROPIC_KEYS if provider == "anthropic" else _OPENAI_KEYS
        if (
            not isinstance(usage, dict)
            or not usage
            or not set(usage).issubset(allowed)
            or any(not _nonnegative(value) for value in usage.values())
        ):
            raise InvalidRequestError("invalid usage counters", source)
    elif "usage" in row:
        raise InvalidRequestError("unexpected usage counters", source)
    return _SidecarRow(
        row["attempt_id"],
        capture_status,
        offset if capture_status == "written" else None,
        cast(UsageStatus, usage_status),
        cast(dict[str, int], usage) if usage_status == "reported" else {},
    )


def _sidecar_rows(
    path: Path, provider: Provider, remaining: int, max_rows: int
) -> tuple[dict[int, _SidecarRow], int, int, int]:
    lines, size = _read_lines(path, remaining)
    offsets: dict[int, _SidecarRow] = {}
    attempt_ids: set[int] = set()
    unassigned = 0
    for _, number, raw in lines:
        source = SourceLocation(str(path), number)
        if number > max_rows:
            raise InvalidRequestError("usage row limit exceeded", source)
        row = _parse_sidecar_row(raw, provider, source)
        if row.attempt_id in attempt_ids:
            raise InvalidRequestError("duplicate usage attempt ID", source)
        attempt_ids.add(row.attempt_id)
        if row.request_offset is None:
            unassigned += 1
        else:
            if row.request_offset in offsets:
                raise InvalidRequestError("duplicate usage request offset", source)
            offsets[row.request_offset] = row
    return offsets, size, len(lines), unassigned


def _metric_counts(values: list[int | None]) -> tuple[int, int, int, int]:
    known = [value for value in values if value is not None]
    return (
        sum(value > 0 for value in known),
        sum(value == 0 for value in known),
        len(values) - len(known),
        sum(known),
    )


def _analyze_usage_with_requests(
    traces: UsageTrace | Iterable[UsageTrace],
    *,
    provider: Provider,
    limits: UsageLimits | None = None,
) -> tuple[UsageResult, tuple[tuple[Path, CanonicalRequest], ...]]:
    """Join traces and retain private request identities for corpus comparison."""
    if provider not in ("anthropic", "openai"):
        raise InvalidRequestError("unsupported usage provider")
    if limits is None:
        limits = UsageLimits()
    if not isinstance(limits, UsageLimits):
        raise InvalidRequestError("invalid usage limits")
    if isinstance(traces, UsageTrace):
        traces = (traces,)
    pairs: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for trace in traces:
        if len(pairs) >= limits.max_traces:
            raise InvalidRequestError("usage trace limit exceeded")
        if not isinstance(trace, UsageTrace):
            raise InvalidRequestError("usage traces must be UsageTrace values")
        request_file, sidecar = _file(trace.requests), _file(trace.sidecar)
        for path in (request_file, sidecar):
            path_key = os.path.normcase(str(path))
            if path_key in seen:
                raise InvalidRequestError(
                    "duplicate usage input file", SourceLocation(str(path))
                )
            seen.add(path_key)
        pairs.append((request_file, sidecar))
    if not pairs:
        raise InvalidRequestError("at least one usage trace is required")
    pairs.sort(key=lambda pair: os.path.normcase(str(pair[0])))
    byte_count = row_count = unassigned_count = 0
    entries: list[tuple[Path, int, CanonicalRequest, _SidecarRow | None]] = []
    for trace_index, (request_path, sidecar_path) in enumerate(pairs, 1):
        request_rows, size = _request_rows(
            request_path,
            provider,
            limits.max_bytes - byte_count,
            limits.max_requests - len(entries),
        )
        byte_count += size
        offsets, size, rows, unassigned = _sidecar_rows(
            sidecar_path,
            provider,
            limits.max_bytes - byte_count,
            limits.max_rows - row_count,
        )
        byte_count += size
        row_count += rows
        unassigned_count += unassigned
        if not set(offsets).issubset(request_rows):
            raise InvalidRequestError(
                "usage offset has no complete request",
                SourceLocation(str(sidecar_path)),
            )
        entries.extend(
            (request_path, trace_index, request, offsets.get(offset))
            for offset, request in request_rows.items()
        )
    keys = sorted({(request.provider, request.model) for _, _, request, _ in entries})
    indexes = {key: index for index, key in enumerate(keys, 1)}
    observations: list[UsageObservation] = []
    for index, (_, trace_index, request, row) in enumerate(entries, 1):
        usage = row.usage if row is not None else {}
        read_key = (
            "cache_read_input_tokens" if provider == "anthropic" else "cached_tokens"
        )
        write_key = "cache_creation_input_tokens" if provider == "anthropic" else None
        observations.append(
            UsageObservation(
                trace_index,
                index,
                indexes[(request.provider, request.model)],
                row.attempt_id if row is not None else None,
                row.usage_status if row is not None else "missing_sidecar",
                usage.get(read_key),
                usage.get(write_key) if write_key is not None else None,
            )
        )
    grouped: dict[int, list[UsageObservation]] = {
        index: [] for index in indexes.values()
    }
    for item in observations:
        grouped[item.group_index].append(item)
    groups: list[UsageGroup] = []
    for group_key in keys:
        group_index = indexes[group_key]
        members = grouped[group_index]
        status_counts = tuple(
            (status, sum(item.usage_status == status for item in members))
            for status in _EVIDENCE_STATUSES
        )
        read = _metric_counts([item.cache_read_tokens for item in members])
        write = _metric_counts([item.cache_write_tokens for item in members])
        groups.append(
            UsageGroup(
                group_index, group_key[0], len(members), status_counts, *read, *write
            )
        )
    result = UsageResult(
        provider,
        len(pairs),
        byte_count,
        len(entries),
        row_count,
        unassigned_count,
        tuple(groups),
        tuple(observations),
    )
    return result, tuple((path, request) for path, _, request, _ in entries)


def analyze_usage(
    traces: UsageTrace | Iterable[UsageTrace],
    *,
    provider: Provider,
    limits: UsageLimits | None = None,
) -> UsageResult:
    """Join complete private trace pairs by exact byte offset, then drop content."""
    result, _ = _analyze_usage_with_requests(traces, provider=provider, limits=limits)
    return result
