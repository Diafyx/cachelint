# Reviewable corpus baselines

An analysis baseline is a versioned, deterministic summary of a known-good
corpus report. It keeps fixed finding codes, safe paths, counts, and advisory
prefix estimates. It omits prompt values, model names, source paths, response
identifiers, free-form finding explanations, and persistent content hashes.

Create a JSON corpus report in a protected directory, review it, then project
the fields you want to keep:

```console
cachelint corpus private-recordings --format json --output private-report.json
cachelint baseline create private-report.json --output reviewed-baseline.json
cachelint baseline inspect reviewed-baseline.json
cachelint baseline inspect reviewed-baseline.json --format json
```

For a report with opt-in usage sidecars, create the report with repeatable
`--usage-trace PROVIDER REQUESTS SIDECAR` arguments as described in the
[corpus guide](corpus.md). Baseline creation reads a report file; it does not
read request logs or contact a provider. Keep raw recordings and sidecars in
their private directory. Review a baseline before committing it: counts and
safe path structure may still reveal workload shape. Do not commit source
recordings or sidecars.

## Contract and compatibility

The bundled [baseline schema v1](../src/cachelint/baseline_v1.schema.json)
defines every field and allowed value. `schema_version` is `1`, and
`baseline_type` is `cachelint.baseline`. `source_report_schema_version` is `1`
for request-only reports and `2` for observed-usage reports. A v1 source gives
each cohort `usage: null` and an empty `observed_findings` list. A v2 source
retains fixed observed finding codes, evidence quality, and known/unknown usage
counts. It drops human-readable observation, cause, and action strings.

`cohorts` contain numeric, provider-scoped identifiers, pair counts, prompt
status, fixed finding codes, and an advisory estimate where available.
`patterns` contain ranks, safe paths, difference kinds, counts, and advisory
estimates. The top-level summary contains only file, request, cohort, pattern,
and observed-finding counts. Estimated tokens are modeled values, not provider
measurements. Unknown usage remains unknown; it is never converted to a miss.

The input must match the bundled corpus report v1 or v2 schema. Unknown report
or baseline schema versions are rejected; the command does not silently
migrate them. Future incompatible baseline layouts require a new schema file
and an explicit migration. `baseline inspect` validates the baseline before
printing either text or canonical JSON. Python callers can use
`baseline_from_report`, `load_baseline`, and `render_baseline_json`.

Baseline JSON uses sorted keys, ascending cohort indexes and ranks, ASCII
escapes, LF line endings, and one final newline. The same validated report
produces identical bytes across supported Python versions and platforms.
Corpus cohort numbering depends on provider, exact model, and normalized input
file order; adding a cohort or renaming source files can change indexes.
Review such changes before treating two baselines as comparable. A baseline
contains no model identifier or content fingerprint to reconnect renumbered
cohorts automatically.

`baseline create` requires `--output`. It writes a temporary file in the
destination directory, flushes it, and atomically replaces the destination
only after validation and rendering succeed. It rejects symlink destinations,
an output identical to the input, and `.jsonl` output names. The source file
must be a regular, UTF-8 JSON file no larger than 32 MiB; duplicate JSON keys,
invalid values, unknown schema versions, and inconsistent counts fail with
status `2` and leave an existing baseline untouched. Unexpected failures use
status `1`. Error text avoids report values, though local paths can still
appear in shell diagnostics outside cachelint.
