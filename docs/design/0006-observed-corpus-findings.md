# ADR 0006: Observed usage findings in corpus reports

- **Status:** Proposed; accepted on merge
- **Issue:** [#40](https://github.com/Diafyx/cachelint/issues/40)
- **Target:** `0.2.0a2`
- **Input contract:** [ADR 0005](0005-observed-usage-join-contract.md)

## Decision

`analyze_observed_corpus` accepts corpus paths and explicit
`CorpusUsageTrace(provider, requests, sidecar)` entries. Each trace request
file must be in the corpus and contain one provider. The existing strict
offset join and sidecar validation apply. The analyzer reads each request
file twice and compares canonicalized requests before attaching counters;
use a stable snapshot of private files. Untraced requests and missing or
unusable counters remain unknown. A sidecar ending in `.usage.jsonl` is
excluded from recursive corpus discovery and rejected as a direct corpus
input.

The fixed cohort reference is the first request in normalized path and line
order. Each later request is paired with it once. Findings can overlap:
one pair can have a modeled divergence, a zero provider read, changed
context, and limited coverage. This report does not try to assign a provider
read to specific prompt bytes. A positive read with modeled divergence is
compatible with reuse of the shared prefix. A zero read with a matching
modeled prefix is an observation with several possible causes, including
expiry, eligibility, or cache-affecting context. It is not proof of any one
cause. Missing counters never count as misses.

The bundled exact-model cache minimum is compared with a local prefix-length
estimate only. The estimate omits provider framing and hidden tokens, and
rules may become outdated. A positive read when the estimate is below the
bundled minimum is labeled as a limit of the model, not a provider error.
Coverage warnings likewise reduce confidence. No price, savings, guaranteed
hit, private model string, prompt value, source path, or content hash is
included in a successful report. Local input errors can contain private
paths and must be handled as private data.

Findings are aggregated by cohort, fixed code, and safe modeled path. They
rank by code priority, count descending, cohort index, then safe path. The
fixed priority favors direct observation: `matching_prefix_no_read`,
`divergent_prefix_no_read`, `read_below_modeled_minimum`,
`read_with_divergence`, `context_changed_no_read`, `short_modeled_prefix`,
`coverage_limited`, `missing_observation`. Counts are pair counts, not
independent events. Explanations are fixed strings with separate observation,
plausible cause, and action fields.

Request-only corpus output remains [schema v1](../../src/cachelint/corpus_report_v1.schema.json).
Passing traces selects [schema v2](../../src/cachelint/corpus_report_v2.schema.json),
which adds sidecar counts, per-cohort usage summaries, and ranked observed
findings. This avoids silently changing the established report contract.

## Provider fields and limits

For Anthropic Messages, the sidecar maps response
[`cache_read_input_tokens` and `cache_creation_input_tokens`](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
to read and write counters. For OpenAI Chat Completions, it maps
[`prompt_tokens_details.cached_tokens`](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
to a read counter. The pinned recorder contract does not provide an OpenAI
write counter. Unknown values stay unknown; known totals are partial when
any values are unknown. Observed counters do not identify a private cache
key or prove which request bytes were reused.
