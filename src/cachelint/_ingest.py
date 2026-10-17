"""Offline ingestion of raw request bodies; canonical values may be sensitive."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Literal

Provider = Literal["anthropic", "openai"]
SegmentKind = Literal["presence", "tool", "text", "block", "message"]

_ANTHROPIC_MARKERS = frozenset({"system", "cache_control"})
_OPENAI_MARKERS = frozenset(
    {
        "max_completion_tokens",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "response_format",
    }
)
_KNOWN_CONTEXT = frozenset(
    {
        "model",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "top_k",
        "stream",
        "stop",
        "stop_sequences",
        "tool_choice",
        "cache_control",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "response_format",
    }
)
_KNOWN_MESSAGE = frozenset({"role", "content", "name", "tool_call_id", "tool_calls"})
_KNOWN_TEXT_BLOCK = frozenset(
    {"type", "text", "cache_control", "prompt_cache_breakpoint"}
)


@dataclass(frozen=True)
class SourceLocation:
    """File and physical line of an input request, when available."""

    path: str
    line: int | None = None

    def __str__(self) -> str:
        return self.path if self.line is None else f"{self.path}:{self.line}"


class InvalidRequestError(ValueError):
    """Invalid raw JSON or unsupported request shape, without source content."""

    def __init__(self, reason: str, source: SourceLocation | None = None) -> None:
        self.reason = reason
        self.source = source
        super().__init__(f"{source}: {reason}" if source is not None else reason)


class IncomparableRequestsError(ValueError):
    """A sequence mixes providers or model names."""

    def __init__(self, reason: str, source: SourceLocation | None = None) -> None:
        self.reason = reason
        self.source = source
        super().__init__(f"{source}: {reason}" if source is not None else reason)


@dataclass(frozen=True)
class Segment:
    """One ordered prompt component; value can contain request content."""

    path: str
    kind: SegmentKind
    value: object


@dataclass(frozen=True)
class CanonicalRequest:
    """An immutable diagnostic view of one raw request body."""

    provider: Provider
    model: str
    segments: tuple[Segment, ...]
    context: Mapping[str, object]
    coverage_warnings: tuple[str, ...]


def _freeze_json(value: object, active: set[int]) -> object:
    """Tag JSON types so booleans, numbers, strings, and null never conflate."""
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, int | float | Decimal):
        try:
            number = Decimal(str(value))
        except (ArithmeticError, ValueError):
            raise InvalidRequestError("invalid JSON number") from None
        if not number.is_finite():
            raise InvalidRequestError("invalid JSON number")
        return ("number", number)
    if isinstance(value, Mapping | list):
        identity = id(value)
        if identity in active:
            raise InvalidRequestError("cyclic request value")
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                if any(not isinstance(key, str) for key in value):
                    raise InvalidRequestError("JSON object keys must be strings")
                return (
                    "object",
                    tuple(
                        (key, _freeze_json(value[key], active)) for key in sorted(value)
                    ),
                )
            return ("array", tuple(_freeze_json(item, active) for item in value))
        finally:
            active.remove(identity)
    raise InvalidRequestError("request contains a non-JSON value")


def _select_provider(body: Mapping[str, object], provider: Provider | None) -> Provider:
    anthropic = any(marker in body for marker in _ANTHROPIC_MARKERS)
    openai = any(marker in body for marker in _OPENAI_MARKERS)
    if provider is not None and provider not in ("anthropic", "openai"):
        raise InvalidRequestError("unsupported provider")
    if anthropic and openai:
        raise InvalidRequestError("conflicting provider markers")
    if (provider == "anthropic" and openai) or (provider == "openai" and anthropic):
        raise InvalidRequestError("provider conflicts with request markers")
    if provider is not None:
        return provider
    if anthropic:
        return "anthropic"
    if openai:
        return "openai"
    raise InvalidRequestError("ambiguous provider; specify anthropic or openai")


def _check_blocks(blocks: list[object], warnings: set[str]) -> None:
    for block in blocks:
        if not isinstance(block, Mapping):
            warnings.add("opaque_content")
        elif block.get("type") != "text" or not isinstance(block.get("text"), str):
            warnings.add("opaque_content")
        elif any(field not in _KNOWN_TEXT_BLOCK for field in block):
            warnings.add("opaque_content")


def canonicalize_request(
    body: Mapping[str, object], *, provider: Provider | None = None
) -> CanonicalRequest:
    """Validate and segment one raw request body without contacting a provider."""
    if not isinstance(body, Mapping):
        raise InvalidRequestError("request must be a JSON object")
    try:
        _freeze_json(body, set())
    except RecursionError:
        raise InvalidRequestError("request nesting is too deep") from None
    selected = _select_provider(body, provider)
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise InvalidRequestError("model must be a nonempty string")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise InvalidRequestError("messages must be an array")

    segments: list[Segment] = []
    warnings: set[str] = set()
    if "tools" in body:
        tools = body["tools"]
        if not isinstance(tools, list):
            raise InvalidRequestError("tools must be an array")
        segments.append(Segment("tools", "presence", ("presence", "array")))
        for index, tool in enumerate(tools):
            segments.append(
                Segment(f"tools[{index}]", "tool", _freeze_json(tool, set()))
            )

    if selected == "anthropic" and "system" in body:
        system = body["system"]
        if isinstance(system, str):
            segments.append(Segment("system", "presence", ("presence", "string")))
            segments.append(Segment("system", "text", _freeze_json(system, set())))
        elif isinstance(system, list):
            segments.append(Segment("system", "presence", ("presence", "array")))
            _check_blocks(system, warnings)
            for index, block in enumerate(system):
                segments.append(
                    Segment(f"system[{index}]", "block", _freeze_json(block, set()))
                )
        else:
            raise InvalidRequestError("system must be a string or array")

    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise InvalidRequestError("messages entries must be JSON objects")
        if not isinstance(message.get("role"), str) or "content" not in message:
            raise InvalidRequestError("message requires a string role and content")
        content = message["content"]
        if isinstance(content, list):
            _check_blocks(content, warnings)
        elif not isinstance(content, str):
            warnings.add("opaque_content")
        if any(field not in _KNOWN_MESSAGE for field in message):
            warnings.add("opaque_content")
        segments.append(
            Segment(f"messages[{index}]", "message", _freeze_json(message, set()))
        )

    context = {
        key: _freeze_json(value, set())
        for key, value in body.items()
        if key not in ("tools", "system", "messages")
    }
    if any(key not in _KNOWN_CONTEXT for key in context):
        warnings.add("unmodeled_context")
    return CanonicalRequest(
        selected,
        model,
        tuple(segments),
        MappingProxyType(context),
        tuple(sorted(warnings)),
    )


class _DuplicateKeyError(ValueError):
    pass


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise InvalidRequestError("invalid JSON number")


def _parse_request(text: str, source: SourceLocation) -> Mapping[str, object]:
    try:
        body = json.loads(
            text,
            parse_int=Decimal,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_pairs,
        )
    except _DuplicateKeyError:
        raise InvalidRequestError("duplicate JSON object key", source) from None
    except InvalidRequestError as error:
        raise InvalidRequestError(error.reason, source) from None
    except (ValueError, RecursionError):
        raise InvalidRequestError("invalid JSON", source) from None
    if not isinstance(body, Mapping):
        raise InvalidRequestError("request must be a JSON object", source)
    return body


def _canonicalize_with_source(
    body: Mapping[str, object], provider: Provider | None, source: SourceLocation
) -> CanonicalRequest:
    try:
        return canonicalize_request(body, provider=provider)
    except InvalidRequestError as error:
        raise InvalidRequestError(error.reason, source) from None


def read_jsonl(
    path: str | Path, *, provider: Provider | None = None
) -> tuple[CanonicalRequest, ...]:
    """Read one raw request per nonblank physical line; this is a CLI internal."""
    source_path = str(path)
    requests: list[CanonicalRequest] = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                source = SourceLocation(source_path, line_number)
                request = _canonicalize_with_source(
                    _parse_request(line, source), provider, source
                )
                if requests and (
                    request.provider != requests[0].provider
                    or request.model != requests[0].model
                ):
                    raise IncomparableRequestsError(
                        "requests must use the same provider and model", source
                    )
                requests.append(request)
    except UnicodeError:
        raise InvalidRequestError(
            "invalid UTF-8", SourceLocation(source_path)
        ) from None
    except OSError:
        raise InvalidRequestError(
            "unreadable input file", SourceLocation(source_path)
        ) from None
    if not requests:
        raise InvalidRequestError("empty input", SourceLocation(source_path))
    return tuple(requests)


def read_json(
    path: str | Path, *, provider: Provider | None = None
) -> CanonicalRequest:
    """Read one raw request object from a UTF-8 file; this is a CLI internal."""
    source = SourceLocation(str(path))
    try:
        with open(path, encoding="utf-8-sig", newline="") as stream:
            contents = stream.read()
    except UnicodeError:
        raise InvalidRequestError("invalid UTF-8", source) from None
    except OSError:
        raise InvalidRequestError("unreadable input file", source) from None
    if not contents.strip():
        raise InvalidRequestError("empty input", source)
    return _canonicalize_with_source(_parse_request(contents, source), provider, source)
