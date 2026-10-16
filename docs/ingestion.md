# Request ingestion

The Python API accepts a **raw request body** for Anthropic Messages or OpenAI
Chat Completions. It works offline and makes no provider cache-hit claim. The
first CLI analysis release will read one such body per nonblank JSONL line.
`analyze` and `diff` are still under development.

```jsonl
{"model":"demo","system":"Follow the instructions.","messages":[{"role":"user","content":"Hello"}]}
{"model":"demo","system":"Follow the instructions.","messages":[{"role":"user","content":"Hello again"}]}
```

Use UTF-8. A BOM is accepted only at the start of the file. Blank physical
lines are skipped but retain their line numbers for errors. Each nonblank line
must be a complete JSON object. Duplicate keys, `NaN`, and `Infinity` are
rejected. Pretty-printed or enveloped requests are not JSONL input.

## Python API

```python
from cachelint import InvalidRequestError, canonicalize_request

body = {
    "model": "demo",
    "messages": [{"role": "user", "content": "Line one\nLine two"}],
}
try:
    request = canonicalize_request(body, provider="openai")
except InvalidRequestError as error:
    print(error)  # A safe reason; no request values.
else:
    print([segment.path for segment in request.segments])  # ['messages[0]']
```

`canonicalize_request` is exported from the package root, along with
`CanonicalRequest`, `Segment`, `SourceLocation`, `InvalidRequestError`, and
`IncomparableRequestsError`. The last error is used when a sequence mixes
providers or models. The JSONL reader is currently a CLI internal, not a
promised public API.

Each request has `provider`, `model`, ordered `segments`, `context`, and
`coverage_warnings`. Each segment has a safe fixed-field `path`, a `kind`, and
an immutable tagged JSON `value`. Tagged values distinguish booleans from
numbers, string content from content arrays, and null from missing fields.
Object key order is ignored, array order is retained, and JSON numbers compare
by decimal value (`1` equals `1.0`). Request context contains every top-level
field outside `tools`, `system`, and `messages`; unknown fields produce
`unmodeled_context`. Unrecognized message fields or content blocks produce
`opaque_content`. The warnings are coverage limits, not provider validation
results.

Auto-detection uses only the top-level markers specified in
[ADR 0001](design/0001-request-comparison-contract.md). A request with both
marker families or neither is ambiguous; pass an explicit provider for the
latter. The minimum accepted shape is a nonempty string `model` and an array
of messages with string `role` and a `content` member. The validator also
requires `tools` to be an array and Anthropic `system` to be a string or array
when those fields are present. Nested content is retained even when it is not
understood; it receives an `opaque_content` warning.

Errors from file ingestion include the filename and physical line where
available, never the failing request line. Python callers already hold the
original values; segment values and context **may contain prompts, credentials,
or customer content**. Do not log or publish those objects. See the
[privacy contract](design/0001-request-comparison-contract.md#paths-and-privacy)
for the later CLI report rules. The [comparison guide](comparison.md) describes
the Python result model built from these canonical requests.
