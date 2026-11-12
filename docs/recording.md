# Record SDK requests locally

`cachelint.recording` is an opt-in Python API for collecting raw request bodies
into analyzer-compatible JSONL. It supports only
`anthropic==1.8.0` Messages at `POST https://api.anthropic.com/v1/messages`
and `openai==3.37.0` Chat Completions at
`POST https://api.openai.com/v1/chat/completions`. Install one or both exact
extras:

```console
python -m pip install 'cachelint[sdk-anthropic]'
python -m pip install 'cachelint[sdk-openai]'
```

The core install still has no runtime dependencies. Importing `cachelint`
does not import a provider SDK or create a recording. Use a dedicated SDK
client and pass `recorder.http_client()` as `http_client=`; existing clients
and process-wide SDK state are untouched.

## Synchronous Anthropic example

```python
from pathlib import Path

from anthropic import Anthropic
from cachelint.recording import RequestRecorder

path = Path("/private/anthropic-requests.jsonl")
with RequestRecorder(path, provider="anthropic") as recorder:
    with Anthropic(http_client=recorder.http_client()) as client:
        client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16,
            messages=[{"role": "user", "content": "Example prompt"}],
        )
    print(recorder.status)
```

## Asynchronous OpenAI example

```python
from pathlib import Path

from openai import AsyncOpenAI
from cachelint.recording import AsyncRequestRecorder


async def collect() -> None:
    path = Path("/private/openai-requests.jsonl")
    async with AsyncRequestRecorder(path, provider="openai") as recorder:
        async with AsyncOpenAI(http_client=recorder.http_client()) as client:
            await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "Example prompt"}],
            )
        print(recorder.status)
```

Close the SDK client before the recorder. The async hook waits for file work;
closing the async recorder waits for in-flight writes. Do not analyze a file
until the recorder closes.

## Opt-in observed cache usage

Set `capture_usage=True` to add a private response-usage sidecar while keeping
the request JSONL analyzer-compatible:

```python
with RequestRecorder(
    path,
    provider="anthropic",
    capture_usage=True,
) as recorder:
    with Anthropic(http_client=recorder.http_client()) as client:
        client.messages.create(...)
    print(recorder.status.usage_written, recorder.status.usage_failed)
```

The sidecar path is the request path plus `.usage.jsonl`; for example,
`/private/anthropic-requests.jsonl.usage.jsonl`. It must not already exist.
Use a fresh path for each recorder session, keep both files together, and do
not edit either before analysis. On POSIX, new sidecars are owner-only (`0600`).
On Windows, they inherit the private parent directory ACL just like the raw
log; `allow_windows_acl_inheritance=True` remains required and does not verify
that ACL. Apply the same retention and disposal rules to both files.

Each sidecar row has schema version 1, a local `attempt_id`, a fixed
`capture_status` and `usage_status`, and only allowlisted nonnegative integer
usage counters. A `written` capture also has `request_offset`, the byte offset
of its corresponding raw JSONL line. This enables exact matching even when
concurrent responses finish out of order. Skipped and failed body captures
have no offset and retain only a fixed `capture_code`. The sidecar never stores
response text, headers, credentials, response IDs, model names, paths,
timestamps, arbitrary metadata, or prompt-derived hashes. It is still private
operational data, not an anonymized report.

Anthropic counters are `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, and `cache_creation_input_tokens`. OpenAI Chat
Completions counters are `prompt_tokens`, `completion_tokens`, `total_tokens`,
and `cached_tokens` from `prompt_tokens_details`. A missing cache-read counter
is not zero. `usage_status="reported"` means at least one allowlisted counter
was present; `absent` means none was available. Other statuses explicitly mark
invalid or oversized responses, streaming responses, HTTP errors, incomplete
reads, and attempts with no matched response. Retries each receive their own
ID. At most 4,096 attempts remain pending in memory; older unresolved attempts
are marked unmatched. A sidecar disk error increments `usage_failed` and emits
one sanitized `usage_write_failed` warning; the provider call and raw request
capture continue. An interrupted process or failed rollback can leave a
partial sidecar line, which consumers must reject.

See [ADR 0004](design/0004-cache-usage-sidecar-contract.md) and the bundled
`usage_sidecar_v1.schema.json` for the complete format and limits. The
sidecar is for the two exact pinned SDK versions and endpoints above. It does
not measure cache behavior when the provider omits counters, and retries are
distinct outbound attempts rather than independent user requests.

## File and privacy rules

Choose an absolute path in an existing private directory. On POSIX, new log
and sidecar lock files are owner-only (mode `0600`), and an existing log with
group or other access is refused. On Windows, the file inherits its parent's
ACL; create a restricted directory first and pass
`allow_windows_acl_inheritance=True`. This flag acknowledges inheritance; it
does **not** check the ACL. The recorder refuses symlink destinations and a
second process using the same path. After a crash, inspect the log before
manually removing a stale `.lock` file. Do not use a directory that untrusted
processes can modify.

Each raw request-log line contains the parsed outbound JSON **body** only. No
request URL, query, headers, API-key configuration, response, or transport
metadata is written to that file. A known credential-like JSON key causes the
entire attempt to be skipped. This is not a secret scanner: API keys embedded
inside arbitrary prompt strings and other sensitive data can still be recorded.
Protect recordings, never commit them, set a retention period, and dispose of
them according to your own policy.

One line represents one outbound attempt. SDK retries can create duplicate
lines; a recorded line does not prove delivery, success, or a cache hit.
Recording does not modify request bytes or retry policy. A write is bounded to
1 MiB and followed by `fsync`, but a crash or disk error can leave an
incomplete final line. Inspect the file after an I/O failure; the analyzer
rejects malformed JSONL rather than silently skipping it.

## Analyze and inspect status

Use a separate file for each provider and model group:

```console
cachelint analyze /private/anthropic-requests.jsonl --provider anthropic
cachelint analyze /private/openai-requests.jsonl --provider openai
```

The provider flag is required for bodies without a provider marker. A single
analysis requires one provider and one exact model. `recorder.status` is an
immutable snapshot with `written`, `skipped`, `failed`, `last_failure_code`,
`usage_written`, and `usage_failed`. It contains no prompt, URL, or file path.

Unsafe setup or an unsupported SDK version raises `RecorderSetupError` or
`UnsupportedSDKError` before a provider call. Once attached, capture
failures do not fail the provider call. A `RecorderWarning` emits each fixed
code once per recorder, while counters record every attempt. Codes are
`unsupported_destination`, `nonbuffered_body`, `invalid_body`,
`credential_field`, `size_limit`, `write_failed`, and `usage_write_failed`.
Warning text has no
request content, destination, path, or exception detail. A warning filter that
treats warnings as errors does not turn these capture failures into provider
errors.

Only the two pinned SDK versions, their default HTTPX2 clients, and the exact
endpoints above are covered. Custom transports, alternate base URLs, Azure,
OpenAI Responses, Anthropic aiohttp, streaming bodies, and other SDK methods
are outside this alpha request-capture contract. Streaming responses are
marked as such in an enabled usage sidecar without reading them. See
[ADR 0003](design/0003-private-sdk-recording-contract.md) for the original
request recorder decision and limitations.
