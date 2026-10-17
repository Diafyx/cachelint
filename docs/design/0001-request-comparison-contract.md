# ADR 0001: Offline request comparison and privacy contract

- **Status:** Accepted
- **Issue:** [#5](https://github.com/Diafyx/cachelint/issues/5)
- **Target:** `0.1.0a1`
- **Reviewed provider documentation:** 2026-10-16

## Context

`cachelint` needs to explain where the beginning of two or more request bodies
stops matching. The first release accepts raw Anthropic Messages and OpenAI Chat
Completions requests saved locally. It does not see provider-rendered prompts,
cache entries, routing, account settings, or response usage.

Anthropic documents a cache hierarchy of `tools`, `system`, then `messages`, as
well as configuration changes that can invalidate cache entries. OpenAI
documents reuse of a rendered prompt prefix containing instructions, tools,
and conversation history; that rendering also includes content outside the
user-supplied request body. Neither documentation is a complete, stable
serialization algorithm for this package to reproduce. See [Anthropic prompt
caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[Anthropic Messages](https://platform.claude.com/docs/en/build-with-claude/working-with-messages),
[OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching),
and the [OpenAI Chat API reference](https://developers.openai.com/api/reference/resources/chat).

The decision below defines **cachelint's diagnostic comparison**, not a provider
cache key or a prediction of cache reads. An equal comparison cannot prove a
cache hit. A difference is evidence to investigate, not proof of a miss.

## Decision

### Input and provider selection

`cachelint analyze PATH` reads UTF-8 JSONL with one raw request object per
nonblank physical line. Blank lines are skipped but count toward reported line
numbers. `cachelint diff LEFT RIGHT` reads one UTF-8 JSON object from each file;
pretty-printed objects are allowed. UTF-8 BOM is accepted only at the start of
a file. Comments, JSON arrays as top-level requests, SDK object reprs, response
objects, and envelopes around a request body are outside this version's input
contract. An empty file is an input error. One valid JSONL request returns an
explicit `insufficient_data` result.

Both commands accept `--provider {auto,anthropic,openai}`; the default is
`auto`. Auto-detection uses only provider-specific top-level markers:

| Provider | Sufficient marker in the raw body |
| --- | --- |
| Anthropic | `system` or `cache_control` |
| OpenAI Chat Completions | `max_completion_tokens`, `prompt_cache_key`, `prompt_cache_options`, `prompt_cache_retention`, or `response_format` |

The marker's **presence**, even with a null value, counts. If both sets appear,
or neither appears, `auto` reports an ambiguity and asks for `--provider`.
Model-name prefixes, message roles, and `max_tokens` are not detection markers.
An explicit provider must not conflict with a marker for the other provider.
All requests in one comparison must have the same provider and exact `model`
string. Mixed-provider or mixed-model inputs are incomparable input errors;
users can split the log and analyze each set separately. The minimum accepted
shape is a JSON object with nonempty string `model` and array `messages` whose
entries are objects with string `role` and a `content` member. Anthropic may
also have top-level `system` as a string or array; either provider may have
top-level `tools` as an array. The validator checks this minimum shape, not
every provider-side rule or model-specific constraint.

### Ordered diagnostic segments

The canonicalizer creates an ordered sequence for each request:

| Provider | Segment order |
| --- | --- |
| Anthropic | `tools` presence and each element, `system` presence and its string or ordered blocks, then each `messages` entry |
| OpenAI Chat Completions | `tools` presence and each element, then each `messages` entry |

The OpenAI order is a **cachelint convention** for reporting tool differences
before conversation differences. It does not assert OpenAI's internal rendering
order. An absent optional field contributes no presence segment; an explicitly
empty array or empty string remains distinct from absence. Array order and block
boundaries are preserved. `content` as a string is distinct from an array with
one text part, even if their visible text matches. A reordered tools array is
therefore a difference.

Within a segment, every JSON field is retained and compared recursively. This
includes nested `cache_control`, tool schemas, roles, content blocks, tool
calls, media references, and fields introduced by a provider after this ADR.
Known text leaves such as `system`, `content`, and a block's `text` can produce
a character offset. Other values are compared structurally as opaque JSON;
cachelint does not decode images, audio, URLs, or base64 payloads. The known
message fields are `role`, `content`, `name`, `tool_call_id`, and `tool_calls`;
known text-block fields are `type`, `text`, `cache_control`, and
`prompt_cache_breakpoint`. Tool-definition objects and their schemas are
compared wholly as structured values. A non-text content block or an unknown
message/text-block field is still compared, with an `opaque_content` coverage
warning. No unknown field is silently treated as stable.

All other top-level fields form a separate **request-context inventory**.
Examples include `model`, token limits, sampling settings, tool selection,
cache options, routing keys, and future fields. Context values are compared
structurally and reported separately from the prompt prefix. Differences in
context do not erase an observed prompt divergence, but the report must say
that cache behavior cannot be inferred from the prompt comparison alone.
The recognized context field names are `model`, `max_tokens`,
`max_completion_tokens`, `temperature`, `top_p`, `top_k`, `stream`, `stop`,
`stop_sequences`, `tool_choice`, `cache_control`, `prompt_cache_key`,
`prompt_cache_options`, `prompt_cache_retention`, and `response_format`.
Unknown top-level fields receive an `unmodeled_context` warning even when their
values match; their names or values are not printed by default. This rule
prevents a future cache-relevant parameter from silently receiving a false
"same request" conclusion.

Object member order is ignored, including inside tool schemas. Array order is
significant. Strings are compared as exact Unicode code point sequences: no
trimming, case folding, Unicode normalization, or whitespace normalization.
JSON numbers are compared by exact decimal value (`1` and `1.0` compare equal);
booleans remain distinct from numbers. Null, missing, empty string, empty
array, and empty object are distinct. Duplicate object keys and non-JSON
numeric constants such as `NaN` are invalid input. This is a semantic JSON
comparison; it cannot detect byte-level serialization differences that a
provider might use internally.

### Divergence and result semantics

For two or more requests, find the longest common prefix of their ordered
segments. The first difference is the earliest segment position where values,
types, or presence differ across requests. Within a shared text leaf, report
the first differing **zero-based Unicode code point offset**. If one text ends
at the common prefix, the offset is its length. For nontext values, report a
safe structural path and no character offset. Insertion or deletion is reported
at the first unmatched array position or end marker, relative to request one.
No later difference is called the first divergence. The same input order always
produces the same result.

The public result has a `prompt_status` of `identical`, `diverged`, or
`insufficient_data`; context differences and coverage warnings are separate
collections. `identical` means only that all modeled prompt segments compare
equal under this ADR. An identical prompt with changed `prompt_cache_key`,
`tool_choice`, or another context field must still display that difference.
For `analyze`, all valid requests are compared as one group in input order;
session or route grouping is deferred. `diff` compares exactly two requests.
Invalid input fails the whole command: no partial success report is emitted.

### Paths and privacy

Default terminal output contains counts, provider, a safe path, difference
kind, character offset when available, context-difference categories, and
coverage warnings. It contains **no prompt excerpts, raw field values, tool
names, credentials, persistent content hashes, or full request bodies**.
Paths may contain only fixed API field names and numeric array indexes, such as
`messages[1].content[0].text`. For arbitrary user-defined object keys, report
the nearest safe parent (for example, `tools[0].input_schema`) with
`object_changed`; do not print the key. A filename or physical line number may
identify an input error, but no source line is echoed. The first release has
no content-reveal flag. Any future excerpt mode needs a separate reviewed
privacy decision and an explicit opt-in.

The package performs no network calls for these commands and writes no report
file by default. Python callers can access the request values they pass in;
they must protect those objects and any logs they create. Requests may contain
personal or customer data even after credentials are removed.

### CLI and Python surface

The planned CLI syntax is:

```text
cachelint analyze PATH [--provider {auto,anthropic,openai}]
cachelint diff LEFT RIGHT [--provider {auto,anthropic,openai}]
```

`--help` and `--version` keep their existing behavior. Exit status `0` means a
valid comparison, including an observed divergence, identical input, or one
valid request with insufficient data. Exit status `2` means invalid arguments,
invalid/ambiguous/unsupported input, or an unreadable input file. Unexpected
internal failures use status `1` with a sanitized error; they must not echo
request values or a traceback in normal CLI operation.

The implementation issues expose this small typed Python surface from the
package root:

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

Provider = Literal["anthropic", "openai"]


def canonicalize_request(
    body: Mapping[str, object], *, provider: Provider | None = None
) -> CanonicalRequest: ...


def compare_requests(
    requests: Sequence[CanonicalRequest],
) -> ComparisonResult: ...
```

`None` uses the same auto-detection rule as the CLI. `CanonicalRequest` exposes
`provider`, `model`, ordered `segments`, and `context`; `Segment` exposes
`path`, `kind`, and `value`. These values can contain sensitive content and are
not safe to log. `ComparisonResult` exposes `prompt_status`, optional
`first_divergence`, context differences, and coverage warnings. A divergence
exposes a safe `path`, `kind`, and optional `offset`, never a raw before/after
value. Public exceptions are `InvalidRequestError` and `IncomparableRequestsError`
with sanitized messages and optional source location. Filesystem/JSONL readers
are CLI internals in `0.1.0a1`; no public log schema is promised beyond the raw
request format above.

## Worked examples

These examples use synthetic content and describe current CLI behavior.

1. **Identical:** With `--provider openai`, two requests with the same
   `messages` and `tools`, but different JSON object member order, return
   `prompt_status=identical` and no first divergence. Context is compared
   separately.
2. **System text:** Anthropic requests with `system` values `"You are helpful."`
   and `"You are careful."` first diverge at `system`, offset `8` (after
   `"You are "`). The report does not print either string.
3. **Tool order:** With `--provider anthropic`, requests with two otherwise
   equal tool objects in opposite order first diverge at `tools[0]`;
   `kind=object_changed`, with no character offset. Tool names are not printed.
4. **Ambiguous provider:** `{"model":"demo","messages":[{"role":"user",
   "content":"Hello"}]}` has no exclusive marker. `--provider auto` returns a
   sanitized ambiguity error with status `2`; an explicit supported provider
   selects the comparison convention.
5. **Malformed JSONL:** If physical line 2 is an incomplete JSON object,
   `analyze requests.jsonl` exits `2` with `requests.jsonl:2: invalid JSON`.
   It does not print the malformed line or a partial analysis.
6. **Unmodeled field:** With `--provider openai`, two requests add a top-level
   `future_prompt_mode` field. It is compared in context, and the report emits
   `unmodeled_context` even if both values match. Prompt segments can still be
   compared, but the report makes no provider cache-hit claim.

## Alternatives considered

- **Raw byte comparison:** Detects formatting and object-member order that may
  not change a logical request, obscuring useful prompt differences.
- **Aggressive normalization:** Sorting arrays or trimming text can hide a real
  difference. Only object-member order and numeric spelling are normalized.
- **Provider cache-key emulation:** Provider rendering, hidden instructions,
  routing, cache lifetime, and model behavior are not completely observable
  from raw request bodies. It would make a misleading guarantee.
- **Fail on every unrecognized field:** Safer in a narrow sense but unusable as
  provider request formats evolve. Compare unknown nested JSON, flag unknown
  context, and state the coverage limit instead.

## Compatibility and migration

There is no released analysis API or log schema to migrate. The planned Python
surface and CLI work on the project's supported Python 3.11–3.14 range without
core runtime dependencies. Changes to this alpha contract between prereleases
must be recorded in release notes. After publication, the documented `0.x`
compatibility policy governs patch and minor releases.

## Consequences and follow-up

The first release favors conservative, reproducible diagnostics and private
default output. Structural equality can miss provider-side differences; extra
context warnings may be noisy. Both limits must remain visible in reports and
documentation.

Implementation issues [#6](https://github.com/Diafyx/cachelint/issues/6),
[#7](https://github.com/Diafyx/cachelint/issues/7), and
[#8](https://github.com/Diafyx/cachelint/issues/8) must follow this accepted
contract or propose an ADR amendment before their PRs merge. Token estimates,
model thresholds, SDK recording, JSON/Markdown output, provider usage
correlation, and content excerpts remain outside `0.1.0a1`.
