# Audit Anthropic cache-control placement

`cachelint breakpoints` checks a private JSONL file of comparable Anthropic
Messages request bodies for modeled stable prefixes and cache-control placement.
It works offline. It does **not** send requests, inspect a provider cache, prove
a hit or miss, or patch a request. The opt-in repair workflow is scoped to
[issue #67](https://github.com/Diafyx/cachelint/issues/67).

```console
cachelint breakpoints /private/anthropic-requests.jsonl
cachelint breakpoints /private/anthropic-requests.jsonl --format json --output /private/breakpoint-audit.json
```

The input must contain at least one Anthropic request with the same exact model
on every nonblank line. A single request gives `insufficient_data`; two or more
permit a stability comparison. The command accepts at most 16 MiB of JSONL.
Its default work limits are 20,000 requests and 200,000 modeled blocks;
`--max-requests` and `--max-blocks` adjust those limits. It rejects input symlinks.
Exit status is 0 for a valid report, 2 for invalid input or output, and 1 for
an unexpected internal failure. `--output` atomically replaces a report after
analysis and refuses symlinks, JSONL destinations, and the input path.

Python users can call the same audit on canonicalized requests:

```python
from cachelint import audit_breakpoints, canonicalize_request

requests = [
    canonicalize_request(body, provider="anthropic") for body in private_request_bodies
]
result = audit_breakpoints(requests)
for finding in result.findings:
    print(finding.code, finding.evidence, finding.path)
```

`BreakpointAuditResult` and `BreakpointLimits` are public. The JSON renderer
returns the separately versioned
[`cachelint.breakpoints` v1 report](../src/cachelint/breakpoint_report_v1.schema.json).
The report has fixed codes, evidence levels, safe structural paths, counts,
modeled block count, and advisory minimum comparison. It omits model names,
source paths, credentials, prompt text, request values, and content hashes.
The raw input remains private. The result still exposes coarse request shape
and count, so review before sharing.

## Reviewed provider rules and limits

The bundled [v1 rule snapshot](../src/cachelint/breakpoint_rules_v1.json) was
reviewed on 2026-11-18 against [Anthropic's prompt-caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
for the Claude API. Anthropic may change these rules; verify the current guide
before using a finding to change production requests.

- A top-level `cache_control` requests automatic caching at the last eligible
  block. Explicit `cache_control` can mark a tool definition, a system content
  block, or a message content block. The audit can reason about nonempty text
  blocks and tool definitions. Images, documents, tool use/results, thinking,
  and other opaque shapes get limited coverage rather than a guessed target.
- `{"type": "ephemeral"}` uses the default five-minute TTL. The reviewed
  extended form adds `"ttl": "1h"`. A longer-lived control must precede a
  shorter-lived one. A maximum of four breakpoints applies; automatic caching
  uses one slot unless the last eligible block already has the same explicit
  TTL. Different TTLs at that last block are a conflict.
- A read searches backward at most 20 block positions from a breakpoint,
  seeking an entry previously **written** at one of those positions. A stable
  block with no earlier cache write is not automatically reusable. The audit's
  `lookback_window` finding uses modeled text/tool positions and cannot know
  prior writes, TTL expiration, routing, or provider rendering.
- The exact-model minimum and token estimate come from the separate
  [length guidance](length-estimates.md). The estimate is advisory, with wide
  uncertainty. Unknown models and estimated short prefixes do not receive a
  placement recommendation.

The audit removes only recognized cache-control metadata while comparing
modeled prompt blocks, so a marker's presence does not itself break the stable
prefix. The advisory minimum comparison measures content through the last
complete cacheable block at the stable boundary; a later shared fragment of a
changing block cannot make that earlier breakpoint appear long enough. It
suppresses placement suggestions when control layouts differ,
request context changes, supported structure is absent, or coverage is opaque.
`adequate_placement` means a marker is at or before the modeled stable boundary
in each request; it is not evidence of provider reuse. `plausible_missed_reuse`
is a candidate for private review, not an automatic fix. Missing an explicit
marker is never treated as a provider error because automatic caching may be
intended. Existing corpus, diagnosis, baseline, and regression report versions
are unchanged.
