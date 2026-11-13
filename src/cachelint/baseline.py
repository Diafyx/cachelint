"""Deterministic, allowlisted projections of versioned corpus reports."""

from __future__ import annotations

import json
import re
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from cachelint._ingest import InvalidRequestError

BASELINE_SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
_USAGE_COUNTS = (
    "request_count",
    "traced_request_count",
    "cache_read_positive_count",
    "cache_read_zero_count",
    "cache_read_unknown_count",
    "known_cache_read_tokens",
    "cache_write_positive_count",
    "cache_write_zero_count",
    "cache_write_unknown_count",
    "known_cache_write_tokens",
)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _load_document(path: str | Path, kind: str) -> dict[str, Any]:
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise InvalidRequestError(f"{kind} must be a regular file")
        with source.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise InvalidRequestError(f"{kind} exceeds 32 MiB")
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, InvalidRequestError):
            raise
        raise InvalidRequestError(f"invalid {kind} JSON") from None
    if not isinstance(document, dict):
        raise InvalidRequestError(f"{kind} must be an object")
    return cast(dict[str, Any], document)


def _schema(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(files("cachelint").joinpath(name).read_text(encoding="utf-8")),
    )


def _same(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _matches(value: object, rule: dict[str, Any], defs: dict[str, Any]) -> bool:
    """Evaluate the small JSON Schema subset used by bundled contracts."""
    reference = rule.get("$ref")
    if reference is not None:
        prefix = "#/$defs/"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            return False
        target = defs.get(reference[len(prefix) :])
        return isinstance(target, dict) and _matches(value, target, defs)
    options = rule.get("oneOf")
    if (
        options is not None
        and sum(_matches(value, item, defs) for item in options) != 1
    ):
        return False
    allowed_type = rule.get("type")
    if allowed_type is not None:
        types = allowed_type if isinstance(allowed_type, list) else [allowed_type]
        valid = {
            "object": lambda: isinstance(value, dict),
            "array": lambda: isinstance(value, list),
            "string": lambda: isinstance(value, str),
            "integer": lambda: type(value) is int,
            "null": lambda: value is None,
        }
        if not any(name in valid and valid[name]() for name in types):
            return False
    if "const" in rule and not _same(value, rule["const"]):
        return False
    if "enum" in rule and not any(_same(value, item) for item in rule["enum"]):
        return False
    if "minimum" in rule and (type(value) is not int or value < rule["minimum"]):
        return False
    if "pattern" in rule and (
        not isinstance(value, str) or re.search(rule["pattern"], value) is None
    ):
        return False
    if rule.get("format") == "date" and value is not None:
        if not isinstance(value, str):
            return False
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            return False
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            return False
    if rule.get("format") == "uri" and value is not None:
        if not isinstance(value, str):
            return False
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        if not parsed.scheme or not parsed.netloc:
            return False
    if isinstance(value, dict):
        properties = rule.get("properties", {})
        if not set(rule.get("required", [])).issubset(value):
            return False
        if rule.get("additionalProperties") is False and not set(value).issubset(
            properties
        ):
            return False
        if any(
            not _matches(item, properties[key], defs)
            for key, item in value.items()
            if key in properties
        ):
            return False
    if isinstance(value, list) and "items" in rule:
        if any(not _matches(item, rule["items"], defs) for item in value):
            return False
    return True


def _validate(document: dict[str, Any], schema_name: str) -> None:
    schema = _schema(schema_name)
    if not _matches(document, schema, schema["$defs"]):
        raise InvalidRequestError(f"document does not match {schema_name}")


def _estimate(value: dict[str, Any] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"tokens": value["tokens"], "method": value["method"]}


def _ordered(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: item[key])
    if [item[key] for item in ordered] != list(range(1, len(ordered) + 1)):
        raise InvalidRequestError(f"noncontiguous {key} values")
    return ordered


def _validate_semantics(document: dict[str, Any]) -> None:
    summary = document["summary"]
    cohorts = _ordered(document["cohorts"], "index")
    patterns = _ordered(document["patterns"], "rank")
    findings = _ordered(document["observed_findings"], "rank")
    if (
        summary["cohort_count"] != len(cohorts)
        or summary["pattern_count"] != len(patterns)
        or summary["observed_finding_count"] != len(findings)
        or summary["request_count"] != sum(item["request_count"] for item in cohorts)
    ):
        raise InvalidRequestError("baseline counts are inconsistent")
    by_index = {item["index"]: item for item in cohorts}
    for cohort in cohorts:
        if (
            cohort["paired_count"] != cohort["request_count"] - 1
            or cohort["identical_count"] + cohort["diverged_count"]
            != cohort["paired_count"]
            or cohort["context_changed_count"] > cohort["paired_count"]
            or cohort["coverage_limited_count"] > cohort["paired_count"]
            or (cohort["usage"] is None)
            != (document["source_report_schema_version"] == 1)
            or cohort["file_count"] > summary["file_count"]
            or cohort["finding_codes"] != sorted(set(cohort["finding_codes"]))
        ):
            raise InvalidRequestError("baseline cohort counts are inconsistent")
        if cohort["usage"] is not None and (
            cohort["usage"]["request_count"] != cohort["request_count"]
            or cohort["usage"]["traced_request_count"] > cohort["request_count"]
        ):
            raise InvalidRequestError("baseline usage counts are inconsistent")
    for item in [*patterns, *findings]:
        target = by_index.get(item["cohort_index"])
        if target is None or target["provider"] != item["provider"]:
            raise InvalidRequestError("baseline cohort reference is invalid")
    if document["source_report_schema_version"] == 1 and findings:
        raise InvalidRequestError("request-only baseline has observed findings")


def baseline_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate a corpus report and project only fixed, reviewable fields."""
    version = report.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise InvalidRequestError("unsupported corpus report schema version")
    _validate(report, f"corpus_report_v{version}.schema.json")
    groups = _ordered(report["groups"], "index")
    patterns = _ordered(report["patterns"], "rank")
    findings = _ordered(report.get("observed_findings", []), "rank")
    summary = report["summary"]
    if (
        summary["cohort_count"] != len(groups)
        or summary["pattern_count"] != len(patterns)
        or summary["request_count"] != sum(group["request_count"] for group in groups)
        or (version == 2 and summary["observed_finding_count"] != len(findings))
    ):
        raise InvalidRequestError("corpus report counts are inconsistent")
    baseline: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "baseline_type": "cachelint.baseline",
        "source_report_schema_version": version,
        "summary": {
            "file_count": summary["file_count"],
            "request_count": summary["request_count"],
            "cohort_count": len(groups),
            "pattern_count": len(patterns),
            "observed_finding_count": len(findings),
        },
        "cohorts": [
            {
                "index": group["index"],
                "provider": group["provider"],
                "file_count": group["file_count"],
                "request_count": group["request_count"],
                "paired_count": group["paired_count"],
                "identical_count": group["identical_count"],
                "diverged_count": group["diverged_count"],
                "context_changed_count": group["context_changed_count"],
                "coverage_limited_count": group["coverage_limited_count"],
                "prompt_status": group["comparison"]["prompt_status"],
                "common_prefix_segments": group["comparison"]["common_prefix_segments"],
                "finding_codes": sorted(set(group["comparison"]["finding_codes"])),
                "prefix_estimate": _estimate(group["comparison"]["prefix_estimate"]),
                "usage": (
                    {
                        **{key: group["usage"][key] for key in _USAGE_COUNTS},
                        "status_counts": group["usage"]["status_counts"],
                    }
                    if version == 2
                    else None
                ),
            }
            for group in groups
        ],
        "patterns": [
            {
                "rank": item["rank"],
                "cohort_index": item["group_index"],
                "provider": item["provider"],
                "category": item["category"],
                "path": item["path"],
                "kind": item["kind"],
                "count": item["count"],
                "common_prefix_segments": item["common_prefix_segments"],
                "prefix_estimate": _estimate(item["prefix_estimate"]),
            }
            for item in patterns
        ],
        "observed_findings": [
            {
                "rank": item["rank"],
                "cohort_index": item["group_index"],
                "provider": item["provider"],
                "code": item["code"],
                "count": item["count"],
                "path": item["path"],
                "evidence_quality": item["evidence_quality"],
            }
            for item in findings
        ],
    }
    _validate(baseline, "baseline_v1.schema.json")
    _validate_semantics(baseline)
    return baseline


def load_corpus_report(path: str | Path) -> dict[str, Any]:
    """Read a bounded UTF-8 corpus report without accepting duplicate keys."""
    return _load_document(path, "corpus report")


def load_baseline(path: str | Path) -> dict[str, Any]:
    """Reject unknown baseline versions and malformed or stale documents."""
    document = _load_document(path, "baseline")
    if document.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise InvalidRequestError("unsupported baseline schema version")
    _validate(document, "baseline_v1.schema.json")
    _validate_semantics(document)
    return document


def render_baseline_json(document: dict[str, Any]) -> str:
    """Render canonical, ASCII-only JSON with one final newline."""
    _validate(document, "baseline_v1.schema.json")
    _validate_semantics(document)
    canonical = dict(document)
    canonical["cohorts"] = _ordered(document["cohorts"], "index")
    canonical["patterns"] = _ordered(document["patterns"], "rank")
    canonical["observed_findings"] = _ordered(document["observed_findings"], "rank")
    return json.dumps(canonical, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def render_baseline_text(document: dict[str, Any]) -> str:
    """Render a compact inspection summary without private source values."""
    _validate(document, "baseline_v1.schema.json")
    _validate_semantics(document)
    summary = document["summary"]
    lines = [
        f"Baseline v{document['schema_version']} from corpus report "
        f"v{document['source_report_schema_version']}",
        f"Files: {summary['file_count']}; requests: {summary['request_count']}; "
        f"cohorts: {summary['cohort_count']}",
        f"Patterns: {summary['pattern_count']}; observed findings: "
        f"{summary['observed_finding_count']}",
    ]
    for group in _ordered(document["cohorts"], "index"):
        lines.append(
            f"Cohort {group['index']} ({group['provider']}): "
            f"{group['request_count']} requests, {group['diverged_count']} divergences"
        )
    for item in _ordered(document["patterns"], "rank"):
        lines.append(
            f"Pattern #{item['rank']} cohort {item['cohort_index']}: "
            f"{item['kind']} at {item['path']} ({item['count']})"
        )
    for item in _ordered(document["observed_findings"], "rank"):
        lines.append(
            f"Observed #{item['rank']} cohort {item['cohort_index']}: "
            f"{item['code']} ({item['count']})"
        )
    return "\n".join(lines) + "\n"
