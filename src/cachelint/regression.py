"""Bounded, opt-in regression gates over safe corpus baseline fields."""

from __future__ import annotations

import json
from typing import Any, cast

from cachelint._ingest import InvalidRequestError
from cachelint.baseline import (
    _load_document,
    _validate,
    _validate_semantics,
    baseline_from_report,
)

POLICY_SCHEMA_VERSION = 1
MAX_POLICY_RULES = 256
MAX_MEASUREMENTS = 50_000
_OBSERVED_METRICS = {
    "cache_read_positive_count",
    "cache_read_zero_count",
    "cache_read_unknown_count",
    "known_cache_read_tokens",
    "cache_write_positive_count",
    "cache_write_zero_count",
    "cache_write_unknown_count",
    "known_cache_write_tokens",
}
_EXPLANATIONS = {
    "missing_cohort": "A baseline cohort has no current cohort at the same index.",
    "new_cohort": "A current cohort has no baseline cohort at the same index.",
    "incomparable_cohort": (
        "The cohort index has a different provider; no metric comparison was made."
    ),
    "missing_metric": (
        "This selected metric is unavailable in one report; unknown is not zero."
    ),
    "incomparable_estimate_method": (
        "Prefix estimates use different methods; token counts were not compared."
    ),
    "regression_exceeds_tolerance": (
        "The selected metric changed beyond its explicit policy tolerance. "
        "Review private source evidence before acting."
    ),
}


def default_policy() -> dict[str, Any]:
    """Return an observation-only policy with no failing gates."""
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_type": "cachelint.regression-policy",
        "cohort_matching": "index_and_provider",
        "on_missing_cohort": "warning",
        "on_new_cohort": "warning",
        "on_incomparable_cohort": "warning",
        "on_missing_metric": "warning",
        "rules": [],
    }


def validate_policy(policy: dict[str, Any]) -> None:
    """Reject unknown versions, duplicate gates, and excessive rule sets."""
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise InvalidRequestError("unsupported regression policy schema version")
    _validate(policy, "regression_policy_v1.schema.json")
    if len(policy["rules"]) > MAX_POLICY_RULES:
        raise InvalidRequestError("regression policy has too many rules")
    identities: set[tuple[int | None, str | None, str, str]] = set()
    for rule in policy["rules"]:
        selector = rule["cohort"]
        identity = (
            None if selector is None else selector["index"],
            None if selector is None else selector["provider"],
            rule["metric"],
            rule["direction"],
        )
        if identity in identities:
            raise InvalidRequestError("regression policy repeats a rule")
        identities.add(identity)


def load_policy(path: str) -> dict[str, Any]:
    """Load and validate a version 1 policy JSON file."""
    policy = _load_document(path, "regression policy")
    validate_policy(policy)
    return policy


def _metric(cohort: dict[str, Any], name: str) -> int | None:
    if name == "prefix_estimate_tokens":
        estimate = cohort["prefix_estimate"]
        return None if estimate is None else cast(int, estimate["tokens"])
    if name in _OBSERVED_METRICS:
        usage = cohort["usage"]
        if usage is None:
            return None
        if name == "known_cache_read_tokens" and usage["cache_read_unknown_count"]:
            return None
        if name == "known_cache_write_tokens" and usage["cache_write_unknown_count"]:
            return None
        return cast(int, usage[name])
    return cast(int, cohort[name])


def _ordered_rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        policy["rules"],
        key=lambda rule: (
            0 if rule["cohort"] is None else rule["cohort"]["index"],
            "" if rule["cohort"] is None else rule["cohort"]["provider"],
            rule["metric"],
            rule["direction"],
        ),
    )


def _finding(
    code: str,
    severity: str,
    index: int,
    provider: str,
    *,
    metric: str | None = None,
    current_provider: str | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "cohort_index": index,
        "provider": provider,
        "current_provider": current_provider,
        "metric": metric,
        "explanation": _EXPLANATIONS[code],
    }


def evaluate_baseline(
    baseline: dict[str, Any],
    current_report: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare two validated, positional cohorts using explicit policy gates."""
    _validate(baseline, "baseline_v1.schema.json")
    _validate_semantics(baseline)
    current = baseline_from_report(current_report)
    effective = default_policy() if policy is None else policy
    validate_policy(effective)
    previous = {item["index"]: item for item in baseline["cohorts"]}
    now = {item["index"]: item for item in current["cohorts"]}
    selectors = {(item["index"], item["provider"]) for item in baseline["cohorts"]}
    for rule in effective["rules"]:
        selector = rule["cohort"]
        if (
            selector is not None
            and (selector["index"], selector["provider"]) not in selectors
        ):
            raise InvalidRequestError("policy cohort selector is absent from baseline")
    measurements: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    matched = 0
    rules = _ordered_rules(effective)

    def emit(
        code: str,
        severity: str,
        index: int,
        provider: str,
        *,
        metric: str | None = None,
        current_provider: str | None = None,
    ) -> None:
        if severity != "ignore":
            findings.append(
                _finding(
                    code,
                    severity,
                    index,
                    provider,
                    metric=metric,
                    current_provider=current_provider,
                )
            )

    for index in sorted(previous.keys() | now.keys()):
        before = previous.get(index)
        after = now.get(index)
        if before is None:
            assert after is not None
            emit("new_cohort", effective["on_new_cohort"], index, after["provider"])
            continue
        if after is None:
            emit(
                "missing_cohort",
                effective["on_missing_cohort"],
                index,
                before["provider"],
            )
            continue
        if before["provider"] != after["provider"]:
            emit(
                "incomparable_cohort",
                effective["on_incomparable_cohort"],
                index,
                before["provider"],
                current_provider=after["provider"],
            )
            continue
        matched += 1
        for rule in rules:
            selector = rule["cohort"]
            if selector is not None and (
                selector["index"] != index or selector["provider"] != before["provider"]
            ):
                continue
            if len(measurements) >= MAX_MEASUREMENTS:
                raise InvalidRequestError(
                    "regression evaluation exceeds measurement limit"
                )
            old = _metric(before, rule["metric"])
            new = _metric(after, rule["metric"])
            severity = rule["severity"]
            if (
                rule["metric"] == "prefix_estimate_tokens"
                and old is not None
                and new is not None
                and before["prefix_estimate"]["method"]
                != after["prefix_estimate"]["method"]
            ):
                assessment = "incomparable"
                status = effective["on_missing_metric"]
                emit(
                    "incomparable_estimate_method",
                    status,
                    index,
                    before["provider"],
                    metric=rule["metric"],
                )
                delta = None
            elif old is None or new is None:
                assessment = "unavailable"
                status = effective["on_missing_metric"]
                emit(
                    "missing_metric",
                    status,
                    index,
                    before["provider"],
                    metric=rule["metric"],
                )
                delta = None
            else:
                delta = new - old
                regression = delta if rule["direction"] == "increase" else -delta
                if regression > rule["tolerance"]:
                    assessment = "regressed"
                    status = severity
                    emit(
                        "regression_exceeds_tolerance",
                        status,
                        index,
                        before["provider"],
                        metric=rule["metric"],
                    )
                elif regression > 0:
                    assessment = "within_tolerance"
                    status = "pass"
                elif regression == 0:
                    assessment = "unchanged"
                    status = "pass"
                else:
                    assessment = "improved"
                    status = "pass"
            measurements.append(
                {
                    "cohort_index": index,
                    "provider": before["provider"],
                    "metric": rule["metric"],
                    "direction": rule["direction"],
                    "tolerance": rule["tolerance"],
                    "baseline_value": old,
                    "current_value": new,
                    "delta": delta,
                    "assessment": assessment,
                    "status": status,
                }
            )
    errors = sum(item["severity"] == "error" for item in findings)
    warnings = sum(item["severity"] == "warning" for item in findings)
    return {
        "schema_version": 1,
        "report_type": "cachelint.regression",
        "status": "fail" if errors else "warning" if warnings else "pass",
        "summary": {
            "baseline_cohort_count": len(previous),
            "current_cohort_count": len(now),
            "matched_cohort_count": matched,
            "measurement_count": len(measurements),
            "warning_count": warnings,
            "error_count": errors,
        },
        "measurements": measurements,
        "findings": findings,
    }


def render_regression_json(result: dict[str, Any]) -> str:
    """Render deterministic, content-free JSON with one final newline."""
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def render_regression_text(result: dict[str, Any]) -> str:
    """Render fixed explanations and selected numeric evidence."""
    summary = result["summary"]
    lines = [
        f"Regression check: {result['status']}",
        f"Cohorts: {summary['matched_cohort_count']} matched, "
        f"{summary['baseline_cohort_count']} baseline, "
        f"{summary['current_cohort_count']} current",
        f"Measurements: {summary['measurement_count']}; "
        f"warnings: {summary['warning_count']}; errors: {summary['error_count']}",
    ]
    for item in result["measurements"]:
        lines.append(
            f"Cohort {item['cohort_index']} {item['provider']} "
            f"{item['metric']}: {item['assessment']} "
            f"({item['baseline_value']} -> {item['current_value']}; "
            f"{item['status']})"
        )
    for item in result["findings"]:
        lines.append(
            f"{item['severity']}: cohort {item['cohort_index']} "
            f"{item['code']}: {item['explanation']}"
        )
    return "\n".join(lines) + "\n"
