# Comparing request prefixes

`compare_requests` compares canonical Anthropic Messages or OpenAI Chat
Completions requests offline. It uses the ordered diagnostic segments from
[ADR 0001](design/0001-request-comparison-contract.md), not either provider's
private cache-key algorithm. A matching modeled prefix cannot prove a cache
hit, and a difference is evidence to investigate rather than proof of a miss.

```python
from cachelint import canonicalize_request, compare_requests

first = canonicalize_request(
    {
        "model": "demo",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hello"}],
    }
)
second = canonicalize_request(
    {
        "model": "demo",
        "system": "You are careful.",
        "messages": [{"role": "user", "content": "Hello"}],
    }
)
result = compare_requests([first, second])
assert result.prompt_status == "diverged"
assert result.first_divergence is not None
assert result.first_divergence.path == "system"
assert result.first_divergence.offset == 8
```

`compare_requests` is exported from the package root. It accepts one or more
`CanonicalRequest` objects. An empty sequence raises `InvalidRequestError`;
mixed providers or exact model strings raise `IncomparableRequestsError`, with
no model values in the error. One request returns `insufficient_data`.

## Result fields

| Field | Meaning |
| --- | --- |
| `provider` | The selected provider, `anthropic` or `openai`. |
| `request_count` | Number of canonical requests supplied. |
| `prompt_status` | `identical`, `diverged`, or `insufficient_data`. |
| `common_prefix_segments` | Number of complete leading segments shared by all requests; zero for one request. |
| `first_divergence` | A `Divergence` with `path`, `kind`, and optional `offset`, or `None`. |
| `context_differences` | `ContextDifference` entries naming fixed context categories; unknown keys collapse to `unmodeled_context`. |
| `coverage_warnings` | Union of `opaque_content` and `unmodeled_context` warnings. |
| `findings` | `Finding` entries with stable codes and fixed explanations of observed evidence. |

The first request is the reference for `inserted` and `removed`. With three or
more requests, the earliest segment difference from that reference wins; input
order breaks ties. An insertion or deletion is reported at the first unmatched
array index or end marker when the shift is clear. Otherwise the first changed
component is reported. Within a message, fixed fields are checked in `role`,
`content`, `name`, `tool_call_id`, `tool_calls` order. Within a text block,
fixed fields are checked in `type`, `text`, `cache_control`,
`prompt_cache_breakpoint` order. Other object keys are still compared, but
their names are not printed.

`Divergence.kind` is one of `text_changed`, `value_changed`, `type_changed`,
`object_changed`, `inserted`, or `removed`. `Finding.code` is one of
`prompt_identical`, `prompt_text_changed`, `prompt_structure_changed`,
`prompt_inserted`, `prompt_removed`, `context_changed`, `coverage_limited`, or
`insufficient_data`. Each finding has a fixed, content-free `message`.

A `text_changed` offset counts zero-based Unicode code points. If one string
ends after the common prefix, the offset is its length. Structural differences
have no offset. `object_changed` on `tools[0]`, for example, reports a tool
definition difference without showing its name or schema key. Context changes
do not erase a prompt divergence. An identical modeled prompt with a changed
`prompt_cache_key` still has a context difference and a finding that provider
cache behavior cannot be inferred from prompt equality alone.

Results contain **no request values, excerpts, tool names, arbitrary object
keys, credentials, or persistent content hashes**. Safe paths contain only
fixed API field names and numeric indexes. The input `CanonicalRequest`
objects do retain sensitive values; protect them and any logs you create.
## CLI report

`cachelint analyze requests.jsonl` compares all nonblank requests as one group.
`cachelint diff first.json second.json` compares exactly two request bodies.
Both commands print the result fields above with fixed labels. For example, an
Anthropic system-text change can produce:

```text
Provider: anthropic
Requests: 2
Prompt status: diverged
Common prefix segments: 1
First divergence: system (text_changed, offset 8)
Context differences: none
Coverage warnings: none
Findings:
  - prompt_text_changed: Text changes at the reported offset in the modeled prompt prefix.
```

`prompt_identical` means modeled segments match, with no cache-hit guarantee.
`prompt_text_changed` identifies a character offset; `prompt_structure_changed`
identifies another changed component; `prompt_inserted` and `prompt_removed`
describe a shift relative to the first request. `context_changed` reports a
separate request-setting difference. `coverage_limited` means opaque content or
unknown context needs review. `insufficient_data` means only one request was
available. A difference is diagnostic evidence, not a verified cache miss.

The report never includes raw values or snippets. Input errors go to standard
error with a filename and physical JSONL line when available, and exit with
status `2`. Valid comparisons exit `0`, including identical and insufficient
results. Unexpected internal failures exit `1` with a generic message.
