# Correlate recorded requests with observed cache usage

Use this offline workflow after recording with `capture_usage=True` in a
supported pinned SDK integration. Keep the raw request JSONL and its
`.usage.jsonl` sidecar from the **same recorder session** unchanged and in
private storage. The sidecar must be the [version 1 format](../src/cachelint/usage_sidecar_v1.schema.json)
written by `anthropic==1.8.0` Messages or `openai==3.37.0` Chat Completions
recorders at their documented endpoints. No provider network request is made.

```python
from cachelint import UsageTrace, analyze_usage
from cachelint.report import usage_report

result = analyze_usage(
    UsageTrace(
        "/private/anthropic-requests.jsonl",
        "/private/anthropic-requests.jsonl.usage.jsonl",
    ),
    provider="anthropic",
)
for group in result.groups:
    print(group.index, group.request_count)
    print(group.cache_read_positive_count, group.cache_read_unknown_count)
for observation in result.observations:
    print(observation.request_index, observation.usage_status)
document = usage_report(result)
```

Pass a list of `UsageTrace` pairs to analyze several sessions as one set of
cohorts. One pair has one provider, specified explicitly. File order is
normalized absolute request-path order; request order is physical line order.
Each cohort groups one exact model, but the result omits model values and
source paths. Adding a cohort can renumber group indexes. A trace index and
attempt ID are local coordinates, not persistent identifiers.

```console
cachelint usage --provider anthropic --trace /private/anthropic-requests.jsonl /private/anthropic-requests.jsonl.usage.jsonl
cachelint usage --provider openai --trace /private/run1.jsonl /private/run1.jsonl.usage.jsonl --trace /private/run2.jsonl /private/run2.jsonl.usage.jsonl --format json --output /private/usage-report.json
```

The command defaults to a text report; `--format json` follows the bundled
[usage report schema v1](../src/cachelint/usage_report_v1.schema.json).
`--output` atomically replaces a report file after validation. It refuses a
`.jsonl` destination, symlink, or a destination equal to an input. The
report still contains private operational metadata, so store and dispose
of it under the same policy as the source recordings. By default, analysis
is bounded to 128 trace pairs, 16 MiB combined input, 20,000 requests, and
40,000 sidecar rows. Adjust `UsageLimits` or the `--max-*` options when needed.
Input errors exit 2; unexpected internal failures exit 1. Python raises
`InvalidRequestError` without a partial result. Error text can include a
local input path and line number and must be treated as private.

## Interpreting the counts

An Anthropic `cache_read_tokens` value comes only from the response usage
field `cache_read_input_tokens`; `cache_write_tokens` comes only from
`cache_creation_input_tokens`. An OpenAI `cache_read_tokens` value comes only
from `prompt_tokens_details.cached_tokens`; its `cache_write_tokens` is always
unknown because this contract has no OpenAI write field. Other usage fields
are not inferred or turned into cache evidence.

Each metric has separate positive, zero, and unknown request counts. A
reported zero is an observation; an absent counter is unknown. The
`known_cache_*_tokens` totals sum reported values only and are **partial**
whenever the corresponding unknown count is nonzero. `usage_status` describes
evidence quality: `reported`, `absent`, `invalid`, `too_large`, `streaming`,
`http_error`, `incomplete`, `unmatched`, or `missing_sidecar`.
Unassigned sidecar attempts are counted separately and never attributed to
a model cohort. Retries are separate attempts, including repeated request
bodies. A partial final line, duplicate attempt ID, duplicate offset, or an
offset without a complete request is rejected; a missing sidecar row is
explicitly unknown rather than a cache miss.

Observed provider counters differ from [modeled prefix estimates](length-estimates.md).
A modeled prefix is a local approximation of request similarity. A positive
provider read counter reports usage for one outbound attempt; neither fact
reveals the provider's private cache key or proves which request bytes were
reused. There are no price or savings estimates. Use
[`cachelint corpus`](corpus.md) for modeled cohort patterns and its opt-in
version 2 report that combines those patterns with this observed evidence.

The paired files have no integrity hash or signed association. Protect both
from edits: a valid replacement request at the same byte offset cannot be
detected by this format. Keep recordings private, set a retention period,
and never commit raw logs, sidecars, or reports. See
[ADR 0005](design/0005-observed-usage-join-contract.md) for the exact join
and validation contract.
