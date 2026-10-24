"""Advisory length estimates for the modeled common prompt prefix."""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Literal, cast

from cachelint._ingest import (
    CanonicalRequest,
    InvalidRequestError,
    Provider,
    Segment,
)

EstimateMethod = Literal["heuristic", "tiktoken"]
MinimumComparison = Literal["estimate_below", "estimate_at_or_above", "unknown"]


@dataclass(frozen=True)
class PrefixEstimate:
    """A content-free, non-authoritative length summary."""

    tokens: int
    method: EstimateMethod
    uncertainty: str
    minimum_cacheable_tokens: int | None
    minimum_comparison: MinimumComparison
    minimum_source: str | None
    rules_reviewed_on: str | None


@dataclass(frozen=True)
class _ModelRule:
    minimum_tokens: int
    source: str
    reviewed_on: str


def _load_model_rules(path: Traversable) -> dict[tuple[Provider, str], _ModelRule]:
    """Load reviewed exact-model guidance; no runtime provider lookup occurs."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported cache-rule data schema")
    reviewed_on = document.get("reviewed_on")
    if not isinstance(reviewed_on, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", reviewed_on
    ):
        raise ValueError("cache-rule review date is missing")
    entries = document.get("rules")
    if not isinstance(entries, list):
        raise ValueError("cache-rule entries are missing")
    rules: dict[tuple[Provider, str], _ModelRule] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid cache-rule entry")
        provider = entry.get("provider")
        model = entry.get("model")
        minimum = entry.get("minimum_tokens")
        source = entry.get("source")
        if (
            provider not in ("anthropic", "openai")
            or not isinstance(model, str)
            or not model
            or type(minimum) is not int
            or minimum <= 0
            or not isinstance(source, str)
            or not source.startswith("https://")
        ):
            raise ValueError("invalid cache-rule entry")
        key = (cast(Provider, provider), model)
        if key in rules:
            raise ValueError("duplicate cache-rule model")
        rules[key] = _ModelRule(minimum, source, reviewed_on)
    return rules


def _model_rule(provider: Provider, model: str) -> _ModelRule | None:
    path = files("cachelint").joinpath("cache_rules.json")
    return _load_model_rules(path).get((provider, model))


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


def _render(value: object) -> str:
    """Render canonical JSON for sizing only; callers never expose this text."""
    tag = _tag(value)
    if tag == "string":
        return json.dumps(_payload(value), ensure_ascii=False)
    if tag == "number":
        return str(cast(Decimal, _payload(value)))
    if tag == "bool":
        return "true" if _payload(value) else "false"
    if tag == "null":
        return "null"
    if tag == "array":
        items = cast(tuple[object, ...], _payload(value))
        return "[" + ",".join(_render(item) for item in items) + "]"
    if tag == "object":
        fields = cast(tuple[tuple[str, object], ...], _payload(value))
        return (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=False) + ":" + _render(item)
                for key, item in fields
            )
            + "}"
        )
    return ""


def _complete_piece(segment: Segment) -> str:
    if segment.kind == "presence":
        return ""
    if segment.kind == "text" and _tag(segment.value) == "string":
        return cast(str, _payload(segment.value))
    return _render(segment.value)


def _partial_text(segment: Segment, path: str, offset: int) -> str:
    """Use only an observed shared text prefix within the changed segment."""
    value = segment.value
    if segment.kind == "text" and path == segment.path:
        return cast(str, _payload(value))[:offset] if _tag(value) == "string" else ""
    if segment.kind == "block" and path == f"{segment.path}.text":
        fields = _fields(value)
        text = fields.get("text") if fields is not None else None
        return cast(str, _payload(text))[:offset] if _tag(text) == "string" else ""
    if segment.kind == "message":
        fields = _fields(value)
        content = fields.get("content") if fields is not None else None
        if path == f"{segment.path}.content" and _tag(content) == "string":
            return cast(str, _payload(content))[:offset]
        match = re.fullmatch(
            re.escape(segment.path) + r"\.content\[(\d+)\]\.text", path
        )
        if match is not None and _tag(content) == "array":
            blocks = cast(tuple[object, ...], _payload(content))
            index = int(match.group(1))
            if index < len(blocks):
                block = _fields(blocks[index])
                text = block.get("text") if block is not None else None
                if _tag(text) == "string":
                    return cast(str, _payload(text))[:offset]
    return ""


def _modeled_prefix(
    reference: CanonicalRequest,
    common_segments: int,
    partial: tuple[str, int] | None,
) -> str:
    pieces = [
        piece
        for segment in reference.segments[:common_segments]
        if (piece := _complete_piece(segment))
    ]
    if partial is not None and common_segments < len(reference.segments):
        piece = _partial_text(reference.segments[common_segments], *partial)
        if piece:
            pieces.append(piece)
    return "\n".join(pieces)


def estimate_prefix(
    reference: CanonicalRequest,
    common_segments: int,
    partial: tuple[str, int] | None,
    method: EstimateMethod,
) -> PrefixEstimate:
    """Size diagnostic content, not provider-rendered or billable tokens."""
    if method not in ("heuristic", "tiktoken"):
        raise InvalidRequestError("unsupported estimate method")
    modeled = _modeled_prefix(reference, common_segments, partial)
    if method == "heuristic":
        tokens = (len(modeled.encode("utf-8", errors="replace")) + 3) // 4
        uncertainty = "wide; content and provider framing can change the count"
    else:
        if reference.provider != "openai":
            raise InvalidRequestError("tiktoken requires an OpenAI request")
        try:
            tiktoken = importlib.import_module("tiktoken")
        except ImportError:
            raise InvalidRequestError("optional tiktoken is not installed") from None
        try:
            encoding = tiktoken.encoding_for_model(reference.model)
        except KeyError:
            raise InvalidRequestError("no tiktoken encoding for this model") from None
        tokens = len(encoding.encode_ordinary(modeled)) if modeled else 0
        uncertainty = (
            "modeled content only; provider framing and hidden tokens excluded"
        )
    rule = _model_rule(reference.provider, reference.model)
    comparison: MinimumComparison = "unknown"
    if rule is not None:
        comparison = (
            "estimate_at_or_above"
            if tokens >= rule.minimum_tokens
            else "estimate_below"
        )
    return PrefixEstimate(
        tokens,
        method,
        uncertainty,
        rule.minimum_tokens if rule is not None else None,
        comparison,
        rule.source if rule is not None else None,
        rule.reviewed_on if rule is not None else None,
    )
