# ADR 0007: bounded prefix-drift diagnosis

## Status

Accepted for the 0.3.0a1 development series. The Python classifier lands in
issue #62; issue #63 will add a CLI and a separately versioned report. This ADR
does not change existing comparison, corpus, baseline, or regression schemas.

## Context

The existing analyzer reports the first modeled prefix difference and, when
provided, provider-reported usage. Users still need help deciding which
request-construction behavior to inspect. [Anthropic's caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
describes tools, system, then messages as its caching hierarchy and explains
automatic and explicit breakpoints. [OpenAI's caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
advises keeping tools and initial instructions stable. Neither guide publishes
a complete stable cache-key algorithm for cachelint to reproduce.

## Decision

The public diagnose_requests function accepts one already-canonicalized
provider/model cohort. It validates that cohort using the existing comparison
contract, then compares each later request with the first request only. A
DiagnosisResult contains provider, counts, and fixed-code DiagnosisFinding
objects. It omits model names, source paths, request values, excerpts,
identifiers, and persistent hashes. Paths use the existing safe canonical
vocabulary. Findings are ordered by descending pair count, then fixed code
order and path. A pair contributes at most one count to each code/path.

The fixed diagnoses are:

| Code | Evidence | Exact trigger |
| --- | --- | --- |
| volatile_timestamp | candidate | The first differing supported text becomes identical when valid ISO date-time tokens with explicit UTC or offset are replaced. |
| volatile_identifier | candidate | The first differing supported text becomes identical when complete UUID-shaped tokens are replaced. |
| tool_order_churn | observed | Tool definitions have the same frozen multiset but a different array order. |
| whitespace_drift | observed | Differing supported text becomes identical after whitespace runs are collapsed. |
| stable_suffix_after_drift | candidate | A text change occurs before at least two identical trailing prompt segments. |
| unclassified_drift | limited | A modeled prompt difference matches none of the supported causes. |
| context_changed | observed | Compared requests have different context, reported separately from prompt differences. |
| coverage_limited | limited | Either request has an opaque-content or unmodeled-context warning; specific causes for that pair are suppressed. |
| insufficient_data | limited | A cohort contains only one request. |

An observed label describes local request differences, not a provider cache
decision. Candidate labels propose where to investigate. Fixed actions ask the
user to review request construction; they never guarantee a hit, savings, or
unchanged model behavior. In particular, a timestamp or UUID-shaped token
could be intentional, whitespace can matter, and moving a segment can change
model behavior. Tool-order advice calls for review before any reordering.

DiagnosisLimits bounds requests, segments, canonical tree nodes, text
characters, and approximate repeated-reference work before comparison.
Defaults match the existing 20,000-request and 16 MiB corpus ceiling while
also bounding unusually large canonical inputs. The API raises a safe
InvalidRequestError when a limit is exceeded. Results retain no input
CanonicalRequest, text, or context object.

## Alternatives and compatibility

Regex-only classification without first-divergence and full-text equivalence
checks was rejected because unrelated changes could be mislabeled as a
timestamp or identifier cause. Comparing every request pair was rejected for
quadratic work and unstable ranking. Automatic request rewriting is deferred;
the classifier only advises. This adds a Python API and does not change any
existing serialized contract or CLI behavior.

## Validation and limits

Synthetic positive and negative controls cover both providers, supported text
blocks, tool order, invalid timestamps, UUID lookalikes, unrelated context,
opaque content, privacy canaries, deterministic counts, and bounds. A later
request is compared with the cohort's first request, so findings describe
that reference relationship rather than all pairwise relationships. Unknown
provider-internal routing, TTL state, content rendering, and cache history
remain unobserved.
