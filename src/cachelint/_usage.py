"""Bounded, content-free projection of provider response usage."""

from __future__ import annotations

import json
from typing import Literal

from cachelint._ingest import Provider

MAX_USAGE_RESPONSE_BYTES = 1024 * 1024
UsageStatus = Literal[
    "reported",
    "absent",
    "invalid",
    "too_large",
    "streaming",
    "http_error",
    "incomplete",
    "unmatched",
]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")


def _counter(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def parse_usage(provider: Provider, raw: bytes) -> tuple[UsageStatus, dict[str, int]]:
    """Return only reviewed nonnegative integer counters from a JSON response."""
    if len(raw) > MAX_USAGE_RESPONSE_BYTES:
        return "too_large", {}
    try:
        response = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError):
        return "invalid", {}
    if not isinstance(response, dict):
        return "invalid", {}
    usage = response.get("usage")
    if usage is None:
        return "absent", {}
    if not isinstance(usage, dict):
        return "invalid", {}
    counters: dict[str, int] = {}
    if provider == "anthropic":
        keys = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        for key in keys:
            if key in usage:
                value = _counter(usage[key])
                if value is None:
                    return "invalid", {}
                counters[key] = value
    else:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage:
                value = _counter(usage[key])
                if value is None:
                    return "invalid", {}
                counters[key] = value
        details = usage.get("prompt_tokens_details")
        if details is not None:
            if not isinstance(details, dict):
                return "invalid", {}
            if "cached_tokens" in details:
                value = _counter(details["cached_tokens"])
                if value is None:
                    return "invalid", {}
                counters["cached_tokens"] = value
    return ("reported", counters) if counters else ("absent", {})


class UsageBuffer:
    """Accumulate at most one MiB of decoded bytes without changing the stream."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._too_large = False

    def add(self, chunk: bytes) -> None:
        if self._too_large:
            return
        if len(self._data) + len(chunk) > MAX_USAGE_RESPONSE_BYTES:
            self._data.clear()
            self._too_large = True
        else:
            self._data.extend(chunk)

    def result(self, provider: Provider) -> tuple[UsageStatus, dict[str, int]]:
        if self._too_large:
            return "too_large", {}
        return parse_usage(provider, bytes(self._data))
