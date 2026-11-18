"""Bounded, content-safe audit of Anthropic prompt-cache breakpoints."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal, cast

from cachelint._compare import _array, _fields, _payload, _tag, compare_requests
from cachelint._estimate import PrefixEstimate, estimate_prefix
from cachelint._ingest import (
    CanonicalRequest,
    IncomparableRequestsError,
    InvalidRequestError,
    Segment,
)
from cachelint.baseline import _validate


def _load_rules() -> tuple[str, str, int, int]:
    """Read the bundled, reviewed Claude API rule snapshot."""
    document = json.loads(
        files("cachelint").joinpath("breakpoint_rules_v1.json").read_text("utf-8")
    )
    if (
        document.get("schema_version") != 1
        or document.get("platform") != "Claude API"
        or document.get("automatic_field") != "cache_control"
        or document.get("explicit_positions")
        != ["tools[*]", "system[*]", "messages[*].content[*]"]
        or document.get("ttl_order") != ["1h", "5m"]
        or type(document.get("max_breakpoints")) is not int
        or type(document.get("lookback_positions")) is not int
        or not isinstance(document.get("reviewed_on"), str)
        or not isinstance(document.get("source"), str)
        or not document["source"].startswith("https://")
    ):
        raise ValueError("invalid breakpoint rule snapshot")
    return (
        document["reviewed_on"],
        document["source"],
        document["max_breakpoints"],
        document["lookback_positions"],
    )


RULES_REVIEWED_ON, RULES_SOURCE, MAX_BREAKPOINTS, LOOKBACK_POSITIONS = _load_rules()

AuditCode = Literal[
    "invalid_control",
    "unsupported_position",
    "too_many_breakpoints",
    "ttl_order",
    "automatic_conflict",
    "automatic_no_target",
    "coverage_limited",
    "context_changed",
    "insufficient_data",
    "unknown_minimum",
    "short_stable_prefix",
    "no_stable_boundary",
    "adequate_placement",
    "plausible_missed_reuse",
    "lookback_window",
]
Evidence = Literal["observed", "candidate", "limited"]

_WORDING: dict[AuditCode, tuple[Evidence, str, str]] = {
    "invalid_control": (
        "observed",
        "A cache control does not match the supported ephemeral form.",
        "Review this private request against the cited provider rules.",
    ),
    "unsupported_position": (
        "observed",
        "A cache control appears outside a supported cacheable block.",
        "Move or remove it only after checking the provider request shape.",
    ),
    "too_many_breakpoints": (
        "observed",
        "The request exceeds the documented four-breakpoint limit.",
        "Review explicit and automatic controls together.",
    ),
    "ttl_order": (
        "observed",
        "A one-hour control follows a five-minute control.",
        "Place longer-lived controls before shorter-lived controls.",
    ),
    "automatic_conflict": (
        "observed",
        "Automatic and final explicit controls use different TTLs.",
        "Choose compatible TTLs after reviewing the final cacheable block.",
    ),
    "automatic_no_target": (
        "limited",
        "No supported automatic cache target was found.",
        "Inspect the private request before relying on automatic caching.",
    ),
    "coverage_limited": (
        "limited",
        "Opaque content or request structure limits placement analysis.",
        "Inspect unsupported content privately; no placement is recommended.",
    ),
    "context_changed": (
        "limited",
        "Request context changes across this cohort.",
        "Review context changes before interpreting modeled prefix reuse.",
    ),
    "insufficient_data": (
        "limited",
        "At least two comparable requests are needed for a stable-prefix audit.",
        "Collect another private request with the same provider and model.",
    ),
    "unknown_minimum": (
        "limited",
        "This exact model has no reviewed cacheable-length minimum.",
        "Check current model and platform rules before recommending placement.",
    ),
    "short_stable_prefix": (
        "limited",
        "The advisory stable-prefix estimate is below the reviewed minimum.",
        "Review the estimate and minimum; a marker alone may not help.",
    ),
    "no_stable_boundary": (
        "limited",
        "No supported cacheable block ends within the modeled stable prefix.",
        "Review private prompt construction before placing a breakpoint.",
    ),
    "adequate_placement": (
        "observed",
        "A control is already at or before the modeled stable boundary.",
        "Validate actual cache usage separately; this is not proof of a hit.",
    ),
    "plausible_missed_reuse": (
        "candidate",
        "No shared control writes the modeled stable boundary.",
        "Review an explicit breakpoint at the reported safe block path.",
    ),
    "lookback_window": (
        "candidate",
        "The automatic target is beyond the modeled 20-position lookback.",
        "Review an earlier explicit breakpoint; prior cache writes are unknown.",
    ),
}


@dataclass(frozen=True)
class BreakpointLimits:
    """Maximum work accepted by the Python audit."""

    max_requests: int = 20_000
    max_blocks: int = 200_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_requests, self.max_blocks)
        ):
            raise InvalidRequestError("breakpoint limits must be positive integers")


@dataclass(frozen=True)
class BreakpointFinding:
    """Fixed wording and a safe structural path; no request value is retained."""

    code: AuditCode
    evidence: Evidence
    path: str
    count: int
    explanation: str
    action: str


@dataclass(frozen=True)
class BreakpointAuditResult:
    """Privacy-safe audit of one Anthropic provider/model cohort."""

    request_count: int
    control_mode: Literal["none", "automatic", "explicit", "mixed", "varied"]
    stable_prefix_blocks: int
    stable_boundary_path: str | None
    prefix_estimate: PrefixEstimate | None
    findings: tuple[BreakpointFinding, ...]


@dataclass(frozen=True)
class _Block:
    path: str
    value: object
    control: object | None
    eligible: bool


@dataclass(frozen=True)
class _View:
    blocks: tuple[_Block, ...]
    automatic: object | None
    mode: Literal["none", "automatic", "explicit", "mixed"]


def _string(value: object) -> str | None:
    return cast(str, _payload(value)) if _tag(value) == "string" else None


def _strip_control(value: object) -> object:
    fields = _fields(value)
    if fields is None or "cache_control" not in fields:
        return value
    return (
        "object",
        tuple((key, item) for key, item in fields.items() if key != "cache_control"),
    )


def _control_ttl(value: object) -> Literal["5m", "1h"] | None:
    fields = _fields(value)
    if fields is None or _string(fields.get("type")) != "ephemeral":
        return None
    if set(fields) == {"type"}:
        return "5m"
    if set(fields) == {"type", "ttl"} and _string(fields["ttl"]) == "1h":
        return "1h"
    return None


def _append_block(
    blocks: list[_Block], path: str, value: object, extra: object = ()
) -> None:
    fields = _fields(value)
    text = _string(fields.get("text")) if fields is not None else _string(value)
    eligible = bool(text) and (fields is None or _string(fields.get("type")) == "text")
    blocks.append(
        _Block(
            path,
            (extra, _strip_control(value)),
            fields.get("cache_control") if fields is not None else None,
            eligible,
        )
    )


def _view(request: CanonicalRequest) -> _View:
    blocks: list[_Block] = []
    for segment in request.segments:
        if segment.kind == "tool":
            fields = _fields(segment.value)
            blocks.append(
                _Block(
                    segment.path,
                    _strip_control(segment.value),
                    fields.get("cache_control") if fields is not None else None,
                    fields is not None,
                )
            )
        elif segment.kind in ("block", "text"):
            _append_block(blocks, segment.path, segment.value)
        elif segment.kind == "message":
            fields = cast(dict[str, object], _fields(segment.value))
            extra = tuple(
                (key, item) for key, item in fields.items() if key != "content"
            )
            content = fields.get("content")
            if _tag(content) == "array":
                for index, item in enumerate(_array(content)):
                    _append_block(
                        blocks, f"{segment.path}.content[{index}]", item, extra
                    )
            else:
                _append_block(blocks, f"{segment.path}.content", content, extra)
    automatic = request.context.get("cache_control")
    explicit = any(block.control is not None for block in blocks)
    mode: Literal["none", "automatic", "explicit", "mixed"] = (
        "mixed"
        if automatic is not None and explicit
        else "automatic"
        if automatic is not None
        else "explicit"
        if explicit
        else "none"
    )
    return _View(tuple(blocks), automatic, mode)


def _without_controls(request: CanonicalRequest) -> CanonicalRequest:
    segments: list[Segment] = []
    for segment in request.segments:
        value = segment.value
        if segment.kind in ("tool", "block"):
            value = _strip_control(value)
        elif segment.kind == "message":
            fields = _fields(value)
            if fields is not None and _tag(fields.get("content")) == "array":
                fields["content"] = (
                    "array",
                    tuple(_strip_control(item) for item in _array(fields["content"])),
                )
                value = ("object", tuple(sorted(fields.items())))
        segments.append(Segment(segment.path, segment.kind, value))
    return CanonicalRequest(
        request.provider,
        request.model,
        tuple(segments),
        {
            key: value
            for key, value in request.context.items()
            if key != "cache_control"
        },
        request.coverage_warnings,
    )


def _estimate_at_boundary(request: CanonicalRequest, boundary: str) -> PrefixEstimate:
    """Estimate through the last complete cacheable block, excluding a later partial."""
    clean = _without_controls(request)
    prefix: list[Segment] = []
    for segment in clean.segments:  # pragma: no branch - boundary comes from _view
        if segment.kind == "message" and boundary.startswith(f"{segment.path}.content"):
            fields = cast(dict[str, object], _fields(segment.value))
            content = fields.get("content")
            if _tag(content) == "array":
                index = int(boundary.rsplit("[", 1)[1][:-1])
                fields["content"] = ("array", _array(content)[: index + 1])
                segment = Segment(
                    segment.path,
                    segment.kind,
                    ("object", tuple(sorted(fields.items()))),
                )
            prefix.append(segment)
            break
        prefix.append(segment)
        if segment.path == boundary:
            break
    return estimate_prefix(
        CanonicalRequest(
            clean.provider,
            clean.model,
            tuple(prefix),
            clean.context,
            clean.coverage_warnings,
        ),
        len(prefix),
        None,
        "heuristic",
    )


def _finding(code: AuditCode, path: str, count: int) -> BreakpointFinding:
    evidence, explanation, action = _WORDING[code]
    return BreakpointFinding(code, evidence, path, count, explanation, action)


def audit_breakpoints(
    requests: Sequence[CanonicalRequest], *, limits: BreakpointLimits | None = None
) -> BreakpointAuditResult:
    """Audit a same-model Anthropic cohort without exposing request values."""
    selected = limits or BreakpointLimits()
    if not isinstance(selected, BreakpointLimits):
        raise InvalidRequestError("expected breakpoint limits")
    group = tuple(requests)
    if not group or len(group) > selected.max_requests:
        raise InvalidRequestError("breakpoint request limit exceeded or cohort empty")
    if any(not isinstance(request, CanonicalRequest) for request in group):
        raise InvalidRequestError("expected canonical requests")
    model = group[0].model
    if any(
        request.provider != "anthropic" or request.model != model for request in group
    ):
        raise IncomparableRequestsError(
            "requests must use Anthropic and the same model"
        )
    views = tuple(_view(request) for request in group)
    if sum(len(view.blocks) for view in views) > selected.max_blocks:
        raise InvalidRequestError("breakpoint block limit exceeded")
    counters: dict[tuple[AuditCode, str], int] = {}

    def add(code: AuditCode, path: str = "request") -> None:
        key = (code, path)
        counters[key] = counters.get(key, 0) + 1

    modes = {view.mode for view in views}
    signatures = {
        (
            view.automatic,
            tuple(
                (block.path, block.control)
                for block in view.blocks
                if block.control is not None
            ),
        )
        for view in views
    }
    mode: Literal["none", "automatic", "explicit", "mixed", "varied"] = (
        next(iter(modes)) if len(modes) == 1 and len(signatures) == 1 else "varied"
    )
    invalid = False
    automatic_positions: list[int] = []
    for request, view in zip(group, views, strict=True):
        controls: list[tuple[int, str, Literal["5m", "1h"]]] = []
        for index, block in enumerate(view.blocks):
            if block.control is None:
                continue
            path = f"{block.path}.cache_control"
            ttl = _control_ttl(block.control)
            if not block.eligible:
                add("unsupported_position", path)
                invalid = True
            if ttl is None:
                add("invalid_control", path)
                invalid = True
            if ttl is not None:
                controls.append((index, path, ttl))
        for segment in request.segments:
            if segment.kind == "message":
                fields = _fields(segment.value)
                if fields is not None and "cache_control" in fields:
                    add("unsupported_position", f"{segment.path}.cache_control")
                    invalid = True
        if view.automatic is not None:
            ttl = _control_ttl(view.automatic)
            if ttl is None:
                add("invalid_control", "cache_control")
                invalid = True
            target = next(
                (
                    index
                    for index in range(len(view.blocks) - 1, -1, -1)
                    if view.blocks[index].eligible
                ),
                None,
            )
            if target is None:
                add("automatic_no_target", "cache_control")
                invalid = True
            elif ttl is not None:
                automatic_positions.append(target)
                explicit_at_target = next(
                    (item for item in controls if item[0] == target), None
                )
                if explicit_at_target is not None and explicit_at_target[2] != ttl:
                    add("automatic_conflict", "cache_control")
                    invalid = True
                elif explicit_at_target is None:
                    controls.append((target, "cache_control", ttl))
        explicit_count = sum(block.control is not None for block in view.blocks)
        if explicit_count > MAX_BREAKPOINTS or (
            view.automatic is not None and explicit_count >= MAX_BREAKPOINTS
        ):
            add("too_many_breakpoints", "cache_control")
            invalid = True
        ordered = sorted(controls, key=lambda item: item[0])
        if any(
            left[2] == "5m" and right[2] == "1h"
            for left, right in zip(ordered, ordered[1:], strict=False)
        ):
            add("ttl_order", ordered[-1][1])
            invalid = True

    stable = 0
    for blocks in zip(*(view.blocks for view in views), strict=False):
        first = blocks[0]
        if any(
            (block.path, block.value) != (first.path, first.value)
            for block in blocks[1:]
        ):
            break
        stable += 1
    boundary = next(
        (
            views[0].blocks[index].path
            for index in range(stable - 1, -1, -1)
            if views[0].blocks[index].eligible
        ),
        None,
    )
    comparison = compare_requests(
        tuple(_without_controls(request) for request in group)
    )
    estimate = (
        _estimate_at_boundary(group[0], boundary)
        if boundary is not None and len(group) >= 2
        else comparison.prefix_estimate
    )
    if len(group) < 2:
        add("insufficient_data")
    elif any(request.coverage_warnings for request in group) or mode == "varied":
        add("coverage_limited")
    elif comparison.context_differences:
        add("context_changed")
    elif not invalid:
        if boundary is None:
            add("no_stable_boundary")
        elif estimate is None or estimate.minimum_cacheable_tokens is None:
            add("unknown_minimum", boundary)
        elif estimate.minimum_comparison == "estimate_below":
            add("short_stable_prefix", boundary)
        else:
            boundary_index = next(
                index
                for index, block in enumerate(views[0].blocks)
                if block.path == boundary
            )
            if all(
                any(
                    block.control is not None
                    for block in view.blocks[: boundary_index + 1]
                )
                or (view.automatic is not None and position <= boundary_index)
                for view, position in zip(
                    views,
                    automatic_positions
                    if len(automatic_positions) == len(views)
                    else [-1] * len(views),
                    strict=True,
                )
            ):
                add("adequate_placement", boundary)
            else:
                add("plausible_missed_reuse", boundary)
                if any(
                    position - boundary_index >= LOOKBACK_POSITIONS
                    for position in automatic_positions
                ):
                    add("lookback_window", boundary)
    findings = tuple(
        _finding(code, path, count) for (code, path), count in sorted(counters.items())
    )
    return BreakpointAuditResult(len(group), mode, stable, boundary, estimate, findings)


def render_breakpoint_json(result: BreakpointAuditResult) -> str:
    """Render a versioned, privacy-safe result with fixed explanation text."""
    if not isinstance(result, BreakpointAuditResult):
        raise InvalidRequestError("expected breakpoint audit result")
    if any(
        finding != _finding(finding.code, finding.path, finding.count)
        for finding in result.findings
    ):
        raise InvalidRequestError("unsupported breakpoint wording")
    document = {
        "schema_version": 1,
        "report_type": "cachelint.breakpoints",
        "rules_reviewed_on": RULES_REVIEWED_ON,
        "request_count": result.request_count,
        "control_mode": result.control_mode,
        "stable_prefix_blocks": result.stable_prefix_blocks,
        "stable_boundary_path": result.stable_boundary_path,
        "minimum_comparison": (
            result.prefix_estimate.minimum_comparison
            if result.prefix_estimate is not None
            else "unknown"
        ),
        "findings": [finding.__dict__ for finding in result.findings],
    }
    _validate(document, "breakpoint_report_v1.schema.json")
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_breakpoint_text(result: BreakpointAuditResult) -> str:
    """Render the same safe audit for terminal review."""
    render_breakpoint_json(result)
    lines = [
        "Anthropic breakpoint audit",
        f"Requests: {result.request_count}",
        f"Control mode: {result.control_mode}",
        f"Modeled stable blocks: {result.stable_prefix_blocks}",
        f"Stable boundary: {result.stable_boundary_path or 'unavailable'}",
        f"Rules reviewed: {RULES_REVIEWED_ON}",
        "Provider cache status: unknown (offline request bodies only)",
    ]
    for finding in result.findings:
        lines.append(
            f"{finding.code} ({finding.evidence}; {finding.count}) at {finding.path}: "
            f"{finding.action}"
        )
    return "\n".join(lines) + "\n"
