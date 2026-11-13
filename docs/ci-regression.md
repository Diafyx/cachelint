# Run a regression gate in CI

The [maintained GitHub Actions example](../.github/workflows/regression-example.yml)
runs on demand with synthetic Anthropic and OpenAI request bodies. Its reviewed
baseline and explicit policy are in [`examples/regression-ci`](../examples/regression-ci).
The fixture passes; changing one repeated request to introduce a new prompt
divergence makes the selected `diverged_count` gate fail with exit status `3`.

Run the same check locally from the repository root:

```console
cachelint corpus examples/regression-ci/current-requests.jsonl --format json --output current-report.json
cachelint baseline check examples/regression-ci/reviewed-baseline.json current-report.json --policy examples/regression-ci/policy.json --format json --output regression.json --summary-output regression.md
```

`regression.json` has `schema_version: 1` and `report_type:
"cachelint.regression"`. The Markdown summary reports the exact policy status,
cohort and measurement counts, and at most ten fixed-code findings. It says
when no metric rules were evaluated. Both outputs omit prompt values, model
names, credentials, response IDs, source paths, and content hashes. They still
expose counts and cohort shape, so review them before sharing. The corpus
report can expose more detail; keep it in the private workspace. Never upload
raw JSONL, usage sidecars, or the current corpus report as public artifacts.

The example uploads only `regression.json` and `regression.md`, with one-day
retention. It appends the Markdown to the GitHub step summary. On policy
failure, both outputs are written before the command returns status `3`; the
job fails and the upload step still runs. If corpus analysis or report export
fails, the workflow writes a fixed failure summary and preserves any already
written safe JSON report. It never turns an incomplete check into a pass.
Exit status `2` means invalid input or policy; `1` means an internal failure.

To use private recordings, add an approved private setup step that produces a
workspace JSONL file, pass its path as the workflow input, and replace the
synthetic baseline and policy with reviewed files for that exact workload.
Keep private files out of commits, logs, cache keys, and artifact paths. If
you use opt-in usage sidecars, pass the same `--usage-trace` arguments that
produced the reviewed baseline. The example's request-only fixture has no
provider-observed usage, so policies on observed metrics need v2 reports.
Anthropic and OpenAI cohorts are matched by position and provider; the
baseline cannot prove that the model or corpus composition stayed the same.
Review changed workloads before treating a passing gate as evidence of stable
provider caching. See the [policy contract](regression.md) for threshold and
missing-data behavior.
