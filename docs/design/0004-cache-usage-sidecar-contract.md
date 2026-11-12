# ADR 0004: Opt-in cache usage sidecar

- **Status:** Proposed; accepted on merge
- **Issue:** [#38](https://github.com/Diafyx/cachelint/issues/38)
- **Target:** `0.2.0a2`
- **Supported SDKs:** `anthropic==1.8.0` Messages and `openai==3.37.0` Chat Completions only

## Context

The request recorder in [ADR 0003](0003-private-sdk-recording-contract.md)
writes analyzer-compatible request bodies before each outbound attempt. A
matching modeled prefix is diagnostic evidence, but the request alone cannot
show whether a provider reported a cache read. Response usage can supply that
observation without storing a response archive. It is operational metadata and
can still be sensitive.

[HTTPX2 response hooks](https://github.com/pydantic/httpx2/blob/main/docs/advanced/event-hooks.md)
run before the response body is read. The two pinned SDKs read normal JSON
responses after the hook. A hook must not eagerly read a streaming response or
replace SDK retry and error behavior.

## Decision

### Opt-in surface and files

`RequestRecorder` and `AsyncRequestRecorder` accept `capture_usage=False` by
default. Setting it to `True` creates a separate file by appending
`.usage.jsonl` to the raw request log path. No sidecar is created by the default
recorder. The raw request JSONL stays exactly as documented in ADR 0003; it has
no envelope or response fields and remains accepted by `cachelint analyze`.

The sidecar must be **new** for each recorder session. A preexisting sidecar,
symlink, or nonregular destination rejects setup. This prevents local attempt
IDs from restarting in an appended sidecar. The recorder holds the same
exclusive `.lock` file while opening both outputs. On POSIX, new sidecars use
owner-only mode `0600`; on Windows they inherit the selected private parent
directory ACL and require `allow_windows_acl_inheritance=True`. That flag does
not verify the ACL. Protect both files and set a retention period; do not commit
them or assume they are anonymized.

### Row contract

Each sidecar line is one compact UTF-8 JSON object conforming to the bundled
`usage_sidecar_v1.schema.json`. It contains only fixed field names, fixed status
codes, nonnegative integer usage counters, and local attempt coordinates:

```json
{"schema_version":1,"attempt_id":1,"capture_status":"written","request_offset":0,"usage_status":"reported","usage":{"input_tokens":120,"cache_read_input_tokens":80}}
```

`attempt_id` starts at one per recorder session. `request_offset` is the byte
offset of the corresponding complete line in the raw request log and appears
only when body capture succeeded. It remains stable when concurrent responses
finish out of order. Callers must keep both files unchanged to use the offset.
Rows are written in response-completion order, followed by unmatched attempts
when the recorder closes; do not infer request order from sidecar line order.
An eligible response without a matching request hook gets a fresh attempt ID
and `capture_status="unmatched"`; usage is never assigned to a different
attempt. If body capture is skipped or fails, its fixed `capture_code` appears
with `capture_status="skipped"` or `"failed"`, and no `request_offset` appears.

Only these response counters are allowlisted:

| Provider | Sidecar usage keys | Provider source |
| --- | --- | --- |
| Anthropic | `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | top-level `usage` object |
| OpenAI | `prompt_tokens`, `completion_tokens`, `total_tokens`, `cached_tokens` | top-level `usage`, with `cached_tokens` from `prompt_tokens_details.cached_tokens` |

Absent counters remain absent; a reported zero remains zero. An unknown usage
field is ignored. An invalid allowlisted value, duplicate JSON key, negative
number, boolean, or non-object usage marks the whole projection `invalid` and
writes no counters. `usage_status="reported"` means at least one allowlisted
counter was present; it does not by itself mean the provider reported a cache
read. `absent` means no allowlisted counters were available. Other fixed
statuses are `too_large`, `streaming`, `http_error`, `incomplete`, and
`unmatched`. The latter means no response hook completed for that attempt,
which may be a transport failure or an interrupted call, not a cache miss.

### Response handling and failures

Only the exact HTTPS POST endpoints and pinned SDK clients from ADR 0003 are
eligible. A scoped request hook assigns a local attempt and records the raw
body as before. A scoped response hook matches `response.request` by object
identity. It never sets request headers or extensions and never sends a
provider call. An HTTP error is marked `http_error`; a `text/event-stream`
response is marked `streaming` without reading or wrapping it. Ordinary JSON
responses are observed as the SDK reads decoded bytes. The wrapper yields the
same chunks and collects at most 1 MiB in memory; larger responses become
`too_large`. Already-buffered mock responses use the same allowlist. If the
SDK stops reading or a stream raises, the row is `incomplete`. No response
text, headers, response IDs, credentials, arbitrary metadata, or content hashes
are written. Error messages and exceptions are never copied to the sidecar.

SDK retries create distinct request attempts and distinct sidecar rows. A
transport failure has no response hook; closing the recorder writes an
`unmatched` row. At most 4,096 requests are kept pending in memory; an older
pending attempt is marked `unmatched` before eviction. The async recorder waits
for request capture workers before close. The application still closes its SDK
client first. `RecorderStatus.usage_written` counts complete sidecar rows and
`usage_failed` counts sidecar write failures. A sidecar append is encoded before
one bounded `os.write` and `fsync`; a short write or disk error attempts to
truncate back, marks sidecar recording broken, and emits one sanitized
`usage_write_failed` warning. The raw request capture proceeds independently.
An abrupt process exit or failed truncation can leave a partial final line;
consumers must reject malformed sidecars rather than silently skipping them.

## Compatibility and follow-up

The default recorder and analyzer input are unchanged. The sidecar is a new
opt-in file and does not prove a cache hit: provider counters may be absent,
partial, or tied to retries. Issue [#39](https://github.com/Diafyx/cachelint/issues/39)
will define how to correlate complete request rows with observed counters.
The sidecar schema is version 1; incompatible changes require a new schema and
documented migration.
