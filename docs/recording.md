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

Each line contains the parsed outbound JSON **body** only. No request URL,
query, headers, API-key configuration, response, or transport metadata is
written. A known credential-like JSON key anywhere in the body causes the
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
immutable snapshot with `written`, `skipped`, `failed`, and
`last_failure_code`. It contains no prompt, URL, or file path.

Unsafe setup or an unsupported SDK version raises `RecorderSetupError` or
`UnsupportedSDKError` before a provider call. Once attached, capture
failures do not fail the provider call. A `RecorderWarning` emits each fixed
code once per recorder, while counters record every attempt. Codes are
`unsupported_destination`, `nonbuffered_body`, `invalid_body`,
`credential_field`, `size_limit`, and `write_failed`. Warning text has no
request content, destination, path, or exception detail. A warning filter that
treats warnings as errors does not turn these capture failures into provider
errors.

Only the two pinned SDK versions, their default HTTPX2 clients, and the exact
endpoints above are covered. Custom transports, alternate base URLs, Azure,
OpenAI Responses, Anthropic aiohttp, streaming bodies, and other SDK methods
are outside this alpha contract. See [ADR 0003](design/0003-private-sdk-recording-contract.md)
for the full decision and limitations.
