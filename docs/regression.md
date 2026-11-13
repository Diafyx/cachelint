# Gate selected corpus regressions

`cachelint baseline check` compares a reviewed baseline with a new corpus
report. It reports changes in safe counts and advisory estimates. It does not
read raw requests, contact a provider, infer cache hits, or claim savings.

```console
cachelint corpus private-recordings --format json --output current-report.json
cachelint baseline check reviewed-baseline.json current-report.json
cachelint baseline check reviewed-baseline.json current-report.json --policy policy.json --format json --output regression.json
```

The first check uses an observation-only default with no metric gates. It may
warn about missing, new, or incomparable cohorts, but exits successfully.
Review the result before opting into a policy. A minimal `policy.json` is:

```json
{
  "schema_version": 1,
  "policy_type": "cachelint.regression-policy",
  "cohort_matching": "index_and_provider",
  "on_missing_cohort": "error",
  "on_new_cohort": "warning",
  "on_incomparable_cohort": "error",
  "on_missing_metric": "warning",
  "rules": [
    {
      "metric": "diverged_count",
      "direction": "increase",
      "tolerance": 0,
      "severity": "error",
      "cohort": null
    },
    {
      "metric": "coverage_limited_count",
      "direction": "increase",
      "tolerance": 0,
      "severity": "warning",
      "cohort": {"index": 1, "provider": "anthropic"}
    }
  ]
}
```

The bundled [policy schema v1](../src/cachelint/regression_policy_v1.schema.json)
requires every field. Unknown versions, fields, metrics, and duplicate rules
are rejected. Each rule selects one fixed metric and compares its numeric
value per matched cohort. `increase` treats `current - baseline` as a
regression; `decrease` treats `baseline - current` as a regression. A change
strictly greater than `tolerance` is `regressed` and receives the chosen
`warning` or `error` severity. A smaller change in that direction is
`within_tolerance`; a change in the other direction is `improved`. An exact
match is `unchanged`. These are arithmetic descriptions of chosen metrics,
not conclusions about provider cache behavior.

Set `cohort` to `null` to apply a rule to every matched cohort. To target one
cohort, use its baseline `index` and `provider`. Such a selector must exist in
the baseline. Cohorts are matched by numeric index and provider. A missing
index is `missing_cohort` or `new_cohort`; a provider change at the same index
is `incomparable_cohort`. The baseline deliberately contains no model name or
content hash, so the same index and provider cannot prove the exact model is
unchanged. Keep corpus inputs and cohort composition stable, inspect changed
workloads, and review cohort numbering before relying on a gate. The policy's
required `cohort_matching` field acknowledges this positional contract.

`on_missing_cohort`, `on_new_cohort`, `on_incomparable_cohort`, and
`on_missing_metric` each accept `ignore`, `warning`, or `error`. An observed
metric is unavailable when either report lacks a usage sidecar. Known token
totals are unavailable when their corresponding unknown count is nonzero;
unknown is never treated as zero. A missing prefix estimate is unavailable.
Prefix token estimates made with different methods are incomparable and use
`on_missing_metric`. Findings contain fixed codes and explanations, never
request values. Rule severity applies only when an available metric exceeds
its tolerance.

The policy supports request and pair counts, modeled divergence, context and
coverage counts, `prefix_estimate_tokens`, observed cache read/write positive,
zero, and unknown counts, and known read/write token totals. The complete
metric enum is in the schema. Counts are not normalized for workload size;
choose thresholds for a reproducible workload and gate request count or
missing cohorts separately where needed. A positive observed read is not
proof of which bytes were reused, and a zero read does not prove a particular
failure cause. Modeled prefix equality is never interpreted as a verified
cache hit.

Exit status `0` means no `error` findings, including a warning-only result.
Status `3` means at least one selected error gate failed. Invalid input or
policy returns `2`; unexpected internal failure returns `1`. `--format json`
produces a version 1 `cachelint.regression` report with fixed status, summary,
measurements, and findings. `--output` atomically writes the report before
returning status `0` or `3`, so CI can retain failure evidence. Output paths
cannot replace any input or end in `.jsonl`.

The evaluator reads bounded baseline and report JSON files, accepts at most
256 policy rules, and stops after 50,000 selected measurements. Change the
policy or split the workload if the limit is reached. This keeps multi-file
corpus checks bounded even when a broad policy targets many cohorts. The
source recordings and usage sidecars remain private; the regression report
exposes only provider, numeric cohort index, selected metric names and counts,
severity, and fixed explanations. Review it before publishing because counts
and cohort shape may still be sensitive.

Python callers can use `load_baseline`, `load_corpus_report`, `load_policy`, and
`evaluate_baseline` to obtain the same result. Omitting a policy uses
`default_policy()` and never creates a failing metric gate.

```python
from cachelint import evaluate_baseline, load_baseline, load_policy
from cachelint.baseline import load_corpus_report

result = evaluate_baseline(
    load_baseline("reviewed-baseline.json"),
    load_corpus_report("current-report.json"),
    load_policy("policy.json"),
)
print(result["status"], result["summary"]["error_count"])
```

For a CI job, add `--summary-output regression.md` to write a bounded Markdown
summary beside the JSON report. See the [CI example](ci-regression.md) for
failure handling, artifact selection, and private-workspace guidance.
