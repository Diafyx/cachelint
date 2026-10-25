# ADR 0003: Private SDK request recording contract

- **Status:** Proposed; accepted on merge
- **Issue:** [#13](https://github.com/Diafyx/cachelint/issues/13)
- **Implementation:** [#14](https://github.com/Diafyx/cachelint/issues/14)
- **Target:** `0.1.0a3`
- **Reviewed SDK and HTTP transport documentation:** 2026-10-25

## Context

`cachelint` currently reads raw Anthropic Messages or OpenAI Chat Completions
request bodies from local JSONL. Exporting those bodies by hand is cumbersome.
An optional SDK recorder can collect them before transmission, but it will
handle prompts and may encounter credentials in user-supplied bodies. It must
not turn on logging for unrelated clients or change a provider request when
recording fails.

Current [Anthropic Python SDK 1.8.0](https://pypi.org/project/anthropic/1.8.0/)
and [OpenAI Python SDK 3.37.0](https://pypi.org/project/openai/3.37.0/) both use
HTTPX2. Their SDK-provided default HTTP clients accept request event hooks.
HTTPX2 calls a [request hook](https://github.com/pydantic/httpx2/blob/main/docs/advanced/event-hooks.md)
after preparing the request and before sending it; async clients require async
hooks. Anthropic's [1.x migration guide](https://github.com/anthropics/anthropic-sdk-python/blob/main/MIGRATION.md)
and OpenAI's [HTTPX2 guide](https://github.com/openai/openai-python/blob/main/httpx2.md)
document the custom-client boundary. On 2026-10-25, a local test with these
exact SDK versions and `httpx2.MockTransport` observed unchanged, buffered JSON
bodies for both supported endpoints in sync and async calls. No live API call
was made. This is feasibility evidence for the hook, not a compatibility claim
for other versions or transports.

The recorder is a capture tool, not a secret scanner. A request body can itself
contain API keys, personal data, customer data, or confidential prompts in
arbitrary text. Users must treat every recording as sensitive even when the
known credential fields below are absent.

## Decision

### Scope and version support

The first recorder supports only these exact SDK versions and request paths:

| Provider | SDK version | Eligible request |
| --- | --- | --- |
| Anthropic | `anthropic==1.8.0` | `POST https://api.anthropic.com/v1/messages` |
| OpenAI | `openai==3.37.0` | `POST https://api.openai.com/v1/chat/completions` |

The target Python range remains 3.11 through 3.14. The implementation adds
separate optional extras, `sdk-anthropic` and `sdk-openai`, pinned to these
reviewed versions. The core install retains zero runtime dependencies. SDKs
are imported lazily only when their recorder is opened. The recorder checks
the installed provider SDK version before creating a log file; a missing or
different version raises a sanitized `UnsupportedSDKError` during setup.
An implementation PR must exercise the hook and package installation on the
supported Python matrix. A later SDK update needs its own reviewed version
change and synthetic hook tests.

The recorder uses a dedicated SDK-provided `DefaultHttpxClient` or
`DefaultAsyncHttpxClient` for Anthropic, or `DefaultHttpx2Client` or
`DefaultAsyncHttpx2Client` for OpenAI. A request event hook is installed only
on that client, which the application explicitly passes as `http_client=` to
a new SDK client. It never patches module-level SDK state, mutates an existing
client, calls `httpx2.alias_httpx()`, or intercepts process-wide traffic. The
user closes the SDK client before closing the recorder. Existing custom HTTP
clients, legacy HTTPX, Anthropic's aiohttp client, OpenAI Responses, Azure or
other custom base URLs, proxies that change the request URL, and other SDK
methods are outside this first contract. An ineligible destination or method
is ignored with a sanitized warning; it is never written to the log.

The hook checks the request method, scheme, host, path, and absence of URL query
parameters, without storing those values. Only the two exact paths above are
eligible. It reads `request.content` **only if already buffered**; it never
calls `read()` or `aread()`, consumes a stream, replaces a body, alters a header,
or inspects a response. A nonbuffered body is skipped and reported. HTTPX2
request hooks can run again for SDK retries or redirects, so the file contains
one row per eligible outbound attempt, not one row per logical SDK call. A row
records an attempt before network transmission; it does not prove acceptance,
response success, or a cache hit. The recorder never initiates or retries a
provider request.

### Recorded format and analyzer compatibility

Each successful capture is exactly one UTF-8 JSON object followed by `\n`.
That object is the parsed, semantic **request body** after SDK preparation.
There is no envelope, timestamp, request ID, provider tag, status code, or
schema marker. The recorder parses JSON strictly, rejecting duplicate keys,
invalid constants, non-object roots, and bodies that the existing
`canonicalize_request` rejects for the selected provider. It serializes a
compact JSON object with non-ASCII text preserved and embedded newlines
escaped. The file is not a byte-for-byte HTTP transcript; object member order
and numeric spelling may change, as allowed by [ADR 0001](0001-request-comparison-contract.md).
Records over 1 MiB of serialized JSON are skipped with a fixed warning. This
limit bounds memory and each file write for the alpha release.

The output is accepted by `cachelint analyze PATH --provider anthropic` or
`--provider openai`. The explicit provider is required because some valid
bodies have no auto-detection marker. Use separate paths for provider and
model groups: the current analyzer rejects mixed providers and models in one
comparison. A future envelope format would require a separate ADR and reader
change. Recording does not change the existing JSONL input contract.

The hook never serializes headers, cookies, authorization, SDK API-key
configuration, URL query strings, transport extensions, responses, errors, or
trace metadata. Before writing, it recursively checks JSON object keys after
case folding and removal of underscores and hyphens. If any key is exactly
`authorization`, `proxyauthorization`, `xapikey`, `apikey`, `accesstoken`,
`refreshtoken`, `clientsecret`, `password`, `secret`, `privatekey`, or `token`,
it **omits the entire attempt** and emits a `credential_field` warning. It
does not write a redacted approximation that the analyzer might mistake for
the sent body. This rule can reject ordinary prompt data with such a key.
If JSON parsing or the credential check fails, the recorder also omits the
attempt; it never falls back to writing unexamined bytes.
It cannot find an API key or other secret embedded in arbitrary string content.
Prompt text and tool definitions in otherwise eligible bodies are written as
supplied. There is no content-reveal CLI switch because the recorder file
itself already contains the content.

### Local storage, concurrency, and atomicity

Recording is off unless the application explicitly opens a recorder with an
absolute, user-selected file path. There is no implicit home-directory log,
network upload, telemetry, rotation, or automatic deletion. The parent
directory must already exist. Setup rejects a directory, a symlink or known
reparse-point destination, and an existing non-regular file. The application
must choose a trusted directory that other users or processes cannot modify.

On POSIX, new log and sidecar lock files are created owner-only (`0600`), and
an existing log with group or other access is rejected. A process-exclusive
sidecar lock is acquired with exclusive creation before opening the log. A
second recorder using the same path fails setup; a stale lock after a crash
requires the owner to inspect the file before manually clearing it. On
Windows, Python's mode bits do not establish a private DACL. The log inherits
the parent directory's ACL. Setup therefore refuses Windows recording unless
the caller explicitly passes `allow_windows_acl_inheritance=True` after
creating a restricted directory; the documentation must not claim that this
flag verifies the ACL. Improving Windows ACL enforcement requires a later
review. Both platforms reject known symlink destinations, but this is not a
defense against a hostile process racing filesystem operations in a directory
it can modify.

Within one recorder, a lock serializes sync threads and async calls. The async
hook offloads file work to a worker thread and awaits its completion; closing
the async recorder waits for in-flight writes. Each record is encoded fully
before acquiring the lock, then appended with one bounded `os.write` and
`fsync`. The writer checks the returned byte count. On a short write or disk
error it attempts to truncate back to the previous length, marks recording
failed, and emits a warning. A line may still exist after an I/O error, and
abrupt process termination can leave a partial final line despite `fsync`.
The contract does not promise crash-safe transactions or exactly-once capture.
Analyze only after the recorder closes; a malformed JSONL line fails the
existing analyzer rather than being silently skipped. Out-of-band writers or
network filesystems are not supported. The recorder never deletes a log on
normal close; users set retention and securely dispose of recordings.

### Public Python surface and failure policy

The implementation exposes typed, opt-in classes from `cachelint.recording`:

```python
with RequestRecorder(path, provider="anthropic") as recorder:
    with Anthropic(http_client=recorder.http_client()) as client:
        client.messages.create(...)
    print(recorder.status)

async with AsyncRequestRecorder(path, provider="openai") as recorder:
    async with AsyncOpenAI(http_client=recorder.http_client()) as client:
        await client.chat.completions.create(...)
```

`path` is an absolute `Path` or string. `provider` is the exact literal
`"anthropic"` or `"openai"`; one recorder owns one provider and one path.
Both constructors have `allow_windows_acl_inheritance=False` by default;
Windows callers must set it to `True` only after restricting the selected
parent directory's ACL.
`http_client()` returns the matching SDK default HTTPX2 client with a scoped
hook and may be called only while the recorder is open. `status` is an
immutable snapshot with counts of written, skipped, and failed attempts plus
the last fixed failure code; it contains no request data or filesystem path.
`RecorderSetupError` covers invalid paths or unsafe file setup, and
`UnsupportedSDKError` covers a missing or unreviewed SDK. Both are raised
before attaching the hook or making a provider call.

Once attached, capture failures never raise an ordinary exception into the
SDK call. They neither change request bytes nor alter the SDK's retry policy.
Instead the recorder increments a status counter and emits a sanitized
`RecorderWarning` once per failure code per recorder. Codes include
`unsupported_destination`, `nonbuffered_body`, `invalid_body`,
`credential_field`, `size_limit`, and `write_failed`. Warnings contain no
request values, URL, headers, path, exception text, or traceback. If the
application suppresses warnings, counters remain inspectable. The hook does
not let a warnings-as-errors filter turn a capture warning into an SDK error.
It does not swallow cancellation, `KeyboardInterrupt`, or `SystemExit`.

### Worked examples

All examples use synthetic values and describe the required implementation
behavior. The API shown above becomes available only in issue #14.

1. **Successful recording:** A dedicated Anthropic client sends a buffered
   `messages.create` request to the default endpoint. The hook writes one raw
   JSON body line before the mock transport receives the unchanged bytes.
   `cachelint analyze file --provider anthropic` accepts the file.
2. **Disabled recording:** An ordinary `Anthropic()` or `OpenAI()` client with
   no recorder has no cachelint hook and creates no file. Importing
   `cachelint` alone does not import either SDK or open a log.
3. **Credential field:** An OpenAI Chat Completions body contains a synthetic
   nested `api_key` key supplied through an extra body field. The hook writes
   no line, increments `skipped`, and emits only `credential_field`. The mock
   transport receives the original request, including that field.
4. **Unsupported SDK:** With `openai==3.36.1`, setup raises
   `UnsupportedSDKError` before opening the file. The application can use its
   normal SDK client without recording or upgrade deliberately.
5. **Disk write failure:** A full disk or failed append increments `failed`
   and emits `write_failed` without a path or body. The already prepared
   provider request reaches the mock transport once; cachelint does not retry
   it. The log is inspected before analysis because the last line may be
   incomplete.
6. **Concurrent calls:** Threads or tasks using one recorder append complete
   lines in lock acquisition order. The order is not promised to match call
   start or response order. A second process attempting the same path fails
   setup rather than interleaving lines.

## Alternatives considered

- Global SDK monkeypatches could affect unrelated clients and silently miss
  the SDKs' HTTPX2 migration.
- Raw HTTP/proxy logging can capture authorization headers, query strings, and
  responses. This recorder sees only buffered request bodies at a dedicated
  client hook.
- Wrapping SDK resource methods would depend on many generated methods and
  might capture arguments before the SDK transforms them. The reviewed hook
  sees the prepared JSON body on the supported paths.
- Mutating a request to redact credentials could change the provider call;
  writing a redacted body could mislead the analyzer. The recorder omits that
  attempt and reports the omission.
- Failing provider calls when the log fails would couple diagnosis to service
  availability. Setup fails early for unsupported configurations; runtime
  capture failures are observable but do not alter provider traffic.

## Compatibility, risks, and follow-up

This ADR changes no installed behavior. Existing offline analysis, CLI input,
and the core dependency-free install continue as documented. The two optional
SDK extras and `cachelint.recording` API are new alpha surfaces. Incompatible
changes after publication require the project's `0.x` minor-release and
migration policy; SDK version expansion requires hook tests and a reviewed
contract update.

The main residual risks are arbitrary secrets inside prompt strings, a
permissive Windows parent ACL, partial lines after a crash, repeated rows from
SDK retries, and provider-side behavior not visible in request bodies. The
implementation PR for [#14](https://github.com/Diafyx/cachelint/issues/14)
must use fake clients, synthetic prompts, no live API calls, and test every
failure path above on supported operating systems. If a supported hook cannot
preserve the request bytes and failure policy, defer that mode rather than
relaxing the contract. After this ADR merges, record its accepted decisions in
issue #14 before starting its implementation PR.
