# Stable-prefix length estimates

`cachelint analyze` and `cachelint diff` report an advisory length for the
modeled common prompt prefix when at least two requests are available. The
Python field is `ComparisonResult.prefix_estimate`; one request returns `None`.
The result contains a number, method, uncertainty note, and optional model
minimum guidance. It contains no request text.

## What is counted

The comparison finds complete shared prompt segments and, for a text change,
the shared characters at the first changed segment when all requests agree on
that text path. The estimator joins the diagnostic values with newlines.
Complete free-text segments contribute text; complete structured segments
contribute compact canonical JSON. Presence markers contribute no text. It
does not model provider-internal message framing, hidden tokens, images,
opaque content, or exact cache-key construction. If a changed message contains
several content blocks, only the shared prefix of its changed text block is
added. The estimate can therefore be low or high relative to provider usage.

The default `heuristic` computes `ceil(UTF-8 byte length / 4)` on that modeled
string. The four-byte rule is a rough average, especially uncertain for code,
JSON, and non-English text. It is deterministic and needs no runtime package.

For OpenAI requests, `--estimate-method tiktoken` uses the optional tokenizer's
`encoding_for_model` and `encode_ordinary` on the **same modeled string**. Install
it with `python -m pip install 'cachelint[tokenizer]'`. If the tokenizer is absent
or does not recognize the exact model, the command exits with input status `2`
and a fixed error. `tiktoken` may download an encoding vocabulary on first use;
the prompt is not sent to a provider. The tokenizer count still excludes
provider framing and is not provider-reported usage. See the
[tiktoken README](https://github.com/openai/tiktoken/blob/main/README.md).

```console
cachelint analyze requests.jsonl
cachelint diff first.json second.json --estimate-method tiktoken --provider openai
```

In Python, pass `estimate_method="tiktoken"` to `compare_requests` to opt in.
For example, `result.prefix_estimate.tokens` is the estimate and
`result.prefix_estimate.method` identifies the method. Do not treat either as
billable input tokens or proof of a cache hit.

## Model minimum guidance

The package includes [`cache_rules.json`](../src/cachelint/cache_rules.json),
a small reviewed list of exact model IDs, documented minimum cacheable token
lengths, source URLs, and a review date. It currently covers Claude Sonnet 4.6,
Claude Opus 4.6, and the listed GPT-5.6 variants. Model aliases, snapshots,
other variants, and changed rules remain **unknown** until a reviewed data
change explicitly covers them. The file's `schema_version` is checked before
loading. Updating provider guidance requires a source and review-date change
with tests. No live provider lookup occurs.

The estimate-to-minimum comparison is advisory: the estimate may omit bytes or
provider tokens, and providers may change eligibility or require other request
settings. A result `estimate_at_or_above` is not a cache-hit prediction;
`estimate_below` is not proof that the provider will reject caching. The
CLI prints the source and review date for a known rule and `unknown` otherwise.
The current rules were reviewed on 2026-10-24 against the
[Anthropic prompt-caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
and [OpenAI prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching).

Provider-reported cached usage is a separate observation from an API response.
`cachelint` analyzes request bodies only, so its CLI always says that
provider-reported cached usage is unavailable. To confirm a hit, inspect the
provider's response usage fields and current documentation for that request.
See [comparison semantics](comparison.md) for the modeled-prefix limits.
