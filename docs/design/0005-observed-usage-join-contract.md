# ADR 0005: Offline request and usage correlation

- **Status:** Proposed; accepted on merge
- **Issue:** [#39](https://github.com/Diafyx/cachelint/issues/39)
- **Target:** `0.2.0a2`
- **Input contract:** [ADR 0004](0004-cache-usage-sidecar-contract.md), schema version 1

## Context

The opt-in SDK recorder writes raw request bodies and a separate usage
sidecar. Its sidecar rows can finish out of order, include retries, and omit
request coordinates when request capture was skipped or failed. A missing
counter is different from a reported zero. Correlation must preserve those
distinctions without exposing private prompt, model, or response values.

## Decision

`analyze_usage` accepts one or more `UsageTrace(requests, sidecar)` pairs and
an explicit `provider`. Each pair represents one recorder session. It reads
the pairs in normalized absolute request-path order and physical line order,
and sorts cohorts by provider and exact model. The resulting `trace_index`,
`request_index`, and `group_index` are local to one analysis and are not
persistent identifiers. Public results omit paths and model strings.

The actual join key is **(trace, byte offset)**: a `written` sidecar row's
`request_offset` must name the start of one complete, nonblank raw request
line in its paired file. The sidecar's `attempt_id` is validated for
uniqueness within that trace and retained as a local coordinate. This is the
only deterministic join available because the analyzer-compatible raw JSONL
has no attempt ID. A sidecar row without an offset is counted as an
unassigned attempt and never attached to a request. A raw request without a
sidecar row gets `usage_status="missing_sidecar"` and unknown counters.
Retries stay as separate outbound attempts even if their bodies are equal.

Validation rejects unsupported schema versions, duplicate JSON keys, unknown
fields or counters, wrong provider counters, duplicate attempt IDs, duplicate
offsets, offsets without complete requests, malformed or incomplete JSONL,
invalid counter types, symlinks, nonregular files, and exceeded limits. No
partial result is returned. Errors may include a private local path and line
number, never input content. The default combined bounds are 128 trace
pairs, 16 MiB of bytes, 20,000 requests, and 40,000 sidecar rows.

Only documented `cache_read_input_tokens` and
`cache_creation_input_tokens` become Anthropic read and write observations.
Only `cached_tokens` (recorded from OpenAI
`prompt_tokens_details.cached_tokens`) becomes an OpenAI read observation;
OpenAI has no normalized write counter here. An absent field stays `None`.
A reported zero is counted separately from a positive value. Cohort totals
sum **known** values only and must be read with the corresponding unknown
count. Status counts distinguish reported, absent, invalid, too large,
streaming, HTTP error, incomplete, unmatched, and missing sidecar evidence.

`cachelint usage` and the Python `usage_report` projection expose a separate
version 1 report. The existing corpus report version 1 and raw request JSONL
remain unchanged. The usage report contains only fixed labels, local numeric
coordinates, status codes, and normalized counters. It does not claim a
provider cache hit, private cache-key identity, cost, or byte-level cache
behavior. Issue [#40](https://github.com/Diafyx/cachelint/issues/40) will
compare this observed evidence with modeled prefix diagnostics.

## Limits

The sidecar carries no request hash or length by design. If someone edits a
raw request into another valid row at the same byte offset, this join cannot
detect the change. Keep the paired files unchanged and protected from
untrusted writers. A missing sidecar row can mean a capture write failure or
an incomplete session; it is not evidence of a cache miss. Request and
sidecar files and local errors remain private, even though the result omits
their contents. Incompatible report changes require a new schema version.
