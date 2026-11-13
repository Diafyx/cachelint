"""Explicit regression policies never infer provider cache outcomes."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from cachelint import (
    CorpusUsageTrace,
    InvalidRequestError,
    analyze_corpus,
    analyze_observed_corpus,
)
from cachelint.baseline import baseline_from_report, render_baseline_json
from cachelint.cli import main
from cachelint.regression import (
    default_policy,
    evaluate_baseline,
    load_policy,
    render_regression_json,
    render_regression_text,
    validate_policy,
)
from cachelint.report import corpus_report

CANARY = "PRIVATE_POLICY_CANARY_MODEL_PROMPT_PATH_HASH"


def _request(provider: str, text: str) -> dict[str, object]:
    body: dict[str, object] = {
        "model": CANARY,
        "messages": [{"role": "user", "content": text}],
    }
    if provider == "anthropic":
        body["system"] = CANARY
    else:
        body["prompt_cache_key"] = CANARY
    return body


def _report(path: Path, provider: str, *texts: str) -> dict[str, Any]:
    path.write_text(
        "".join(json.dumps(_request(provider, text)) + "\n" for text in texts),
        encoding="utf-8",
    )
    return cast(dict[str, Any], corpus_report(analyze_corpus(path)))


def _observed(path: Path) -> dict[str, Any]:
    requests = path / "observed.jsonl"
    sidecar = path / "observed.jsonl.usage.jsonl"
    first = (json.dumps(_request("anthropic", CANARY + "A")) + "\n").encode()
    second = (json.dumps(_request("anthropic", CANARY + "B")) + "\n").encode()
    requests.write_bytes(first + second)
    sidecar.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "attempt_id": index,
                    "capture_status": "written",
                    "request_offset": offset,
                    "usage_status": "reported",
                    "usage": {"cache_read_input_tokens": value},
                }
            )
            + "\n"
            for index, offset, value in ((1, 0, 0), (2, len(first), 7))
        ),
        encoding="utf-8",
    )
    return cast(
        dict[str, Any],
        corpus_report(
            analyze_observed_corpus(
                requests,
                usage_traces=[CorpusUsageTrace("anthropic", requests, sidecar)],
            )
        ),
    )


def _rule(
    metric: str = "diverged_count",
    *,
    direction: str = "increase",
    tolerance: int = 0,
    severity: str = "error",
    cohort: dict[str, object] | None = None,
) -> dict[str, Any]:
    return {
        "metric": metric,
        "direction": direction,
        "tolerance": tolerance,
        "severity": severity,
        "cohort": cohort,
    }


def _policy(*rules: dict[str, Any], **actions: str) -> dict[str, Any]:
    policy = default_policy()
    policy["rules"] = list(rules)
    policy.update(actions)
    return policy


def test_default_never_fails_on_modeled_change_and_cli_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_report = _report(tmp_path / "before.jsonl", "anthropic", "A", "A")
    current_report = _report(tmp_path / "after.jsonl", "anthropic", "A", "B")
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(render_baseline_json(baseline_from_report(baseline_report)))
    current.write_text(json.dumps(current_report), encoding="utf-8")
    assert (
        main(["baseline", "check", str(baseline), str(current), "--format", "json"])
        == 0
    )
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert document["status"] == "pass"
    assert document["measurements"] == []
    assert document["findings"] == []
    assert CANARY not in output.out
    assert "cache hit" not in output.out.lower()


@pytest.mark.parametrize(
    ("old_texts", "new_texts", "tolerance", "severity", "assessment", "status"),
    [
        (("A", "B"), ("A", "A"), 0, "error", "improved", "pass"),
        (("A", "B"), ("A", "B"), 0, "error", "unchanged", "pass"),
        (("A", "A"), ("A", "B"), 1, "error", "within_tolerance", "pass"),
        (("A", "A"), ("A", "B"), 0, "warning", "regressed", "warning"),
        (("A", "A"), ("A", "B"), 0, "error", "regressed", "fail"),
    ],
)
def test_explicit_metric_directions_and_thresholds(
    tmp_path: Path,
    old_texts: tuple[str, ...],
    new_texts: tuple[str, ...],
    tolerance: int,
    severity: str,
    assessment: str,
    status: str,
) -> None:
    baseline = baseline_from_report(
        _report(tmp_path / "old.jsonl", "anthropic", *old_texts)
    )
    current = _report(tmp_path / "new.jsonl", "anthropic", *new_texts)
    result = evaluate_baseline(
        baseline, current, _policy(_rule(tolerance=tolerance, severity=severity))
    )
    assert result["status"] == status
    assert result["measurements"][0]["assessment"] == assessment
    assert (result["summary"]["error_count"] > 0) == (status == "fail")
    assert CANARY not in render_regression_json(result)
    assert CANARY not in render_regression_text(result)


def test_decrease_gate_and_cli_failure_writes_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = baseline_from_report(
        _report(tmp_path / "old.jsonl", "anthropic", "A", "B")
    )
    current = _report(tmp_path / "new.jsonl", "anthropic", "A")
    policy = _policy(_rule("request_count", direction="decrease"))
    old_path = tmp_path / "baseline.json"
    new_path = tmp_path / "current.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "result.json"
    old_path.write_text(render_baseline_json(baseline))
    new_path.write_text(json.dumps(current), encoding="utf-8")
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    assert (
        main(
            [
                "baseline",
                "check",
                str(old_path),
                str(new_path),
                "--policy",
                str(policy_path),
                "--format",
                "json",
                "--output",
                str(output_path),
            ]
        )
        == 3
    )
    assert capsys.readouterr() == ("", "")
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["status"] == "fail"
    assert result["measurements"][0]["delta"] == -1
    assert result["measurements"][0]["assessment"] == "regressed"
    assert result["findings"][0]["code"] == "regression_exceeds_tolerance"
    assert not list(tmp_path.glob(".cachelint-report-*.tmp"))


def test_missing_new_and_incomparable_cohorts(tmp_path: Path) -> None:
    anthropic = _report(tmp_path / "anthropic.jsonl", "anthropic", "A", "A")
    openai = _report(tmp_path / "openai.jsonl", "openai", "A", "A")
    mixed_path = tmp_path / "mixed.jsonl"
    mixed_path.write_text(
        json.dumps(_request("anthropic", "A"))
        + "\n"
        + json.dumps(_request("openai", "A"))
        + "\n",
        encoding="utf-8",
    )
    mixed = cast(dict[str, Any], corpus_report(analyze_corpus(mixed_path)))

    policy = _policy(
        on_missing_cohort="error", on_new_cohort="error", on_incomparable_cohort="error"
    )
    missing = evaluate_baseline(baseline_from_report(mixed), anthropic, policy)
    assert {item["code"] for item in missing["findings"]} == {"missing_cohort"}
    assert missing["status"] == "fail"
    added = evaluate_baseline(baseline_from_report(anthropic), mixed, policy)
    assert {item["code"] for item in added["findings"]} == {"new_cohort"}
    assert added["status"] == "fail"
    incomparable = evaluate_baseline(baseline_from_report(anthropic), openai, policy)
    assert {item["code"] for item in incomparable["findings"]} == {
        "incomparable_cohort"
    }
    assert incomparable["summary"]["matched_cohort_count"] == 0
    assert incomparable["findings"][0]["current_provider"] == "openai"


@pytest.mark.parametrize("action", ["ignore", "warning", "error"])
def test_missing_observed_metric_is_not_zero(tmp_path: Path, action: str) -> None:
    report = _report(tmp_path / "one.jsonl", "anthropic", "A", "B")
    result = evaluate_baseline(
        baseline_from_report(report),
        report,
        _policy(_rule("cache_read_zero_count"), on_missing_metric=action),
    )
    assert result["measurements"][0]["assessment"] == "unavailable"
    assert result["measurements"][0]["baseline_value"] is None
    assert (
        result["status"]
        == {"ignore": "pass", "warning": "warning", "error": "fail"}[action]
    )
    assert len(result["findings"]) == (0 if action == "ignore" else 1)


def test_observed_counts_partial_totals_and_estimate_methods(tmp_path: Path) -> None:
    report = _observed(tmp_path)
    baseline = baseline_from_report(report)
    current = deepcopy(report)
    current["groups"][0]["usage"]["cache_read_zero_count"] += 1
    result = evaluate_baseline(
        baseline, current, _policy(_rule("cache_read_zero_count"))
    )
    assert result["status"] == "fail"
    assert result["measurements"][0]["current_value"] == 2

    baseline["cohorts"][0]["usage"]["cache_read_unknown_count"] = 1
    result = evaluate_baseline(
        baseline, report, _policy(_rule("known_cache_read_tokens"))
    )
    assert result["measurements"][0]["assessment"] == "unavailable"
    baseline["cohorts"][0]["usage"]["cache_write_unknown_count"] = 1
    result = evaluate_baseline(
        baseline, report, _policy(_rule("known_cache_write_tokens"))
    )
    assert result["measurements"][0]["assessment"] == "unavailable"

    baseline = baseline_from_report(report)
    current = deepcopy(report)
    current["groups"][0]["comparison"]["prefix_estimate"]["method"] = "tiktoken"
    result = evaluate_baseline(
        baseline, current, _policy(_rule("prefix_estimate_tokens"))
    )
    assert result["measurements"][0]["assessment"] == "incomparable"
    assert result["findings"][0]["code"] == "incomparable_estimate_method"


def test_policy_selector_order_duplicate_and_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report(tmp_path / "one.jsonl", "anthropic", "A", "B")
    baseline = baseline_from_report(report)
    selector = {"index": 1, "provider": "anthropic"}
    rules = [
        _rule("request_count", cohort=selector),
        _rule("diverged_count", cohort=selector),
    ]
    first = evaluate_baseline(baseline, report, _policy(*rules))
    second = evaluate_baseline(baseline, report, _policy(*reversed(rules)))
    assert render_regression_json(first) == render_regression_json(second)
    assert [item["metric"] for item in first["measurements"]] == [
        "diverged_count",
        "request_count",
    ]
    with pytest.raises(InvalidRequestError, match="repeats a rule"):
        validate_policy(_policy(rules[0], rules[0]))
    with pytest.raises(InvalidRequestError, match="selector is absent"):
        evaluate_baseline(
            baseline, report, _policy(_rule(cohort={"index": 2, "provider": "openai"}))
        )
    monkeypatch.setattr("cachelint.regression.MAX_POLICY_RULES", 1)
    with pytest.raises(InvalidRequestError, match="too many rules"):
        validate_policy(_policy(*rules))
    monkeypatch.setattr("cachelint.regression.MAX_MEASUREMENTS", 0)
    with pytest.raises(InvalidRequestError, match="measurement limit"):
        evaluate_baseline(baseline, report, _policy(rules[0]))


def test_selector_skips_other_matched_cohorts(tmp_path: Path) -> None:
    source = tmp_path / "mixed.jsonl"
    source.write_text(
        json.dumps(_request("anthropic", "A"))
        + "\n"
        + json.dumps(_request("openai", "A"))
        + "\n",
        encoding="utf-8",
    )
    report = cast(dict[str, Any], corpus_report(analyze_corpus(source)))
    result = evaluate_baseline(
        baseline_from_report(report),
        report,
        _policy(_rule("request_count", cohort={"index": 1, "provider": "anthropic"})),
    )
    assert result["summary"]["matched_cohort_count"] == 2
    assert result["summary"]["measurement_count"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(schema_version=2),
        lambda item: item.update(extra=CANARY),
        lambda item: item.update(cohort_matching="model_name"),
        lambda item: item["rules"].append(_rule("PRIVATE_METRIC")),
        lambda item: item["rules"].append(_rule(tolerance=-1)),
        lambda item: item["rules"].append(_rule(severity="fatal")),
        lambda item: item["rules"].append(
            _rule(cohort={"index": 0, "provider": "anthropic"})
        ),
    ],
)
def test_invalid_and_stale_policy_files_rejected(
    tmp_path: Path, mutate: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = default_policy()
    mutate(policy)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_policy(str(path))
    baseline_report = _report(tmp_path / "before.jsonl", "anthropic", "A", "A")
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        render_baseline_json(baseline_from_report(baseline_report))
    )
    current_path = tmp_path / "current.json"
    current_path.write_text(json.dumps(baseline_report), encoding="utf-8")
    result_path = tmp_path / "result.json"
    result_path.write_text("keep", encoding="utf-8")
    assert (
        main(
            [
                "baseline",
                "check",
                str(baseline_path),
                str(current_path),
                "--policy",
                str(path),
                "--output",
                str(result_path),
            ]
        )
        == 2
    )
    assert capsys.readouterr().out == ""
    assert result_path.read_text(encoding="utf-8") == "keep"


def test_output_guard_and_fixed_explanation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(tmp_path / "one.jsonl", "anthropic", "A", "B")
    baseline = baseline_from_report(report)
    baseline_path = tmp_path / "baseline.json"
    report_path = tmp_path / "current.json"
    baseline_path.write_text(render_baseline_json(baseline))
    report_path.write_text(json.dumps(report), encoding="utf-8")
    policy = _policy(_rule("diverged_count", direction="decrease"))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    assert (
        main(
            [
                "baseline",
                "check",
                str(baseline_path),
                str(report_path),
                "--policy",
                str(policy_path),
                "--output",
                str(policy_path),
            ]
        )
        == 2
    )
    assert "must not replace an input" in capsys.readouterr().err
    assert (
        main(
            [
                "baseline",
                "check",
                str(baseline_path),
                str(report_path),
                "--policy",
                str(policy_path),
            ]
        )
        == 0
    )
    assert "Regression check: pass" in capsys.readouterr().out
