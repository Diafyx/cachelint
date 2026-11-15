# Diagnose recurring prefix drift

The development Python API classifies supported differences within one
Anthropic Messages or OpenAI Chat Completions cohort. It runs offline on
canonicalized request bodies. Issue #63 will add the command-line interface
and a versioned diagnosis report; the current published 0.2.0a3 release does
not contain this API.

~~~python
from cachelint import canonicalize_request, diagnose_requests


def request(system: str):
    return canonicalize_request(
        {
            "model": "demo",
            "system": system,
            "messages": [{"role": "user", "content": "same question"}],
        }
    )


result = diagnose_requests(
    [
        request("Checked 2026-11-14T09:12:13Z"),
        request("Checked 2026-11-15T09:12:13Z"),
    ]
)
assert [(f.code, f.evidence, f.path) for f in result.findings] == [
    ("volatile_timestamp", "candidate", "system")
]
~~~

All requests in one call must use the same provider and exact model. The first
request is the fixed reference; every other request is compared with it.
Findings count how many of those later requests match each fixed code and
safe path. They are ordered by count, then code and path. A single request
returns insufficient_data. Identical prompt segments with changed request
settings produce context_changed, not a claimed prompt cause.

The supported candidates are ISO date-time changes with an explicit timezone,
complete UUID-shaped changes, reordered identical tool definitions,
whitespace-run drift, and text changes followed by at least two identical
prompt segments. Unsupported content suppresses specific causes for its pair
and yields coverage_limited. Other differences are unclassified_drift.
DiagnosisLimits can lower the request, segment, node, text, and work ceilings
for a private application.

Each finding contains a fixed explanation and next action. An observed level
means cachelint saw a local request difference. A candidate level means a
pattern suggests an investigation. A limited level means the classifier lacks
enough supported evidence. None proves a provider cache hit or miss. Check
provider usage counters and your own request construction before changing
prompts or tool order. [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
and [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
describe provider-specific constraints that may evolve.

CanonicalRequest objects retain private prompt values. Keep them and any
source JSONL in a protected workspace. DiagnosisResult has no model name,
request values, excerpts, source paths, or content hashes, but paths and
counts can still reveal workload shape. Review them before sharing.
The [decision record](design/0007-prefix-drift-diagnosis-contract.md) defines
the exact code triggers and known blind spots.
