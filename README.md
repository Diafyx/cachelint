# cachelint

cachelint is a Python package for investigating LLM prompt-cache misses. It compares request bodies offline and shows where the modeled prompt prefix changes.

**Status:** early alpha. The package ingests and compares raw request bodies
through the Python API and CLI, with advisory stable-prefix length estimates.
SDK recording is planned for a later alpha release under the
[private recording contract](docs/design/0003-private-sdk-recording-contract.md).
See the [0.1.0a2 release notes](docs/releases/0.1.0a2.md).

## Requirements and current usage

Python 3.11 or newer is required. The initial CI matrix covers Python 3.11 through 3.14.

For development from this checkout:

```console
python -m pip install --upgrade pip
python -m pip install --group dev -e .
cachelint --help
cachelint --version
```

The `cachelint` distribution, `cachelint` import package, and `cachelint` CLI share the same name. The core package has no runtime dependencies. The optional OpenAI tokenizer integration can be installed with `python -m pip install 'cachelint[tokenizer]'`.

## Analyze files from the CLI

Save one complete raw request body per nonblank line in a UTF-8 JSONL file, then run:

```console
cachelint analyze requests.jsonl
cachelint analyze requests.jsonl --provider openai
cachelint analyze requests.jsonl --estimate-method tiktoken --provider openai
```

To compare two separate JSON files, each containing one complete request body (pretty printing is allowed):

```console
cachelint diff first.json second.json
cachelint diff first.json second.json --provider anthropic
```

The default provider choice is `auto`. Bodies without a provider-specific marker need `--provider anthropic` or `--provider openai`; conflicting markers are rejected. A comparison requires the same provider and exact model in every request. Output includes the provider, request count, prompt status, common prefix segment count, first safe divergence path and character offset when available, an advisory stable-prefix length estimate, documented exact-model minimum when known, context categories, coverage warnings, and fixed findings. It never prints request values or excerpts. `cachelint analyze` compares every valid JSONL request as one group; one request reports `insufficient_data` and has no length estimate. See the [length estimate guide](docs/length-estimates.md) for the heuristic, optional tokenizer, and model-rule limits.

Exit status `0` means a valid analysis, including a divergence or insufficient data. Status `2` means invalid arguments or input, including unreadable files; status `1` means an unexpected internal failure. Invalid input produces no partial report. A matching modeled prefix does not guarantee a provider cache hit. See the [ingestion guide](docs/ingestion.md) and [comparison guide](docs/comparison.md) for input rules and finding meanings.

## Request ingestion in Python

`canonicalize_request` validates one raw Anthropic Messages or OpenAI Chat Completions request body and returns ordered prompt segments, separate request context, and coverage warnings:

```python
from cachelint import canonicalize_request

request = {
    "model": "demo",
    "system": "Follow the instructions.",
    "messages": [{"role": "user", "content": "Hello"}],
}
canonical = canonicalize_request(request)
print(canonical.provider)  # anthropic
print([segment.path for segment in canonical.segments])
# ['system', 'system', 'messages[0]']
```

Use `provider="anthropic"` or `provider="openai"` when a body has no provider-specific marker. `InvalidRequestError` reports invalid shapes without echoing request values. The segment `value` and context inventory **do retain request content**; protect them as you would the original body. See the [ingestion guide](docs/ingestion.md) for accepted JSONL input, validation rules, and limits.

## Comparing request prefixes in Python

`compare_requests` accepts one or more canonical requests of the same provider and exact model. It returns the first difference in the modeled prompt prefix and reports context changes separately:

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
print(result.prompt_status)  # diverged
assert result.first_divergence is not None
print(result.first_divergence.path, result.first_divergence.offset)  # system 8
```

The result contains safe paths, difference kinds, character offsets where available, an advisory `prefix_estimate`, context categories, coverage warnings, and evidence-based findings. It contains no prompt excerpts or raw values. See the [comparison guide](docs/comparison.md) and [length estimate guide](docs/length-estimates.md) for result semantics and privacy limits.

## Analysis scope

The `0.1.0a1` release introduced offline analysis for Anthropic Messages and OpenAI Chat Completions request bodies. `0.1.0a2` adds advisory length guidance and an optional OpenAI tokenizer. The accepted canonicalization contract and privacy handling guide the commands. A matching text prefix is evidence for diagnosis, not a guarantee of a provider cache hit.

The [request comparison and privacy contract](docs/design/0001-request-comparison-contract.md) defines the first release's input and diagnostic behavior.

Request logs can contain prompts, credentials, personal data, and customer content. Do not commit raw logs or paste them into public issues.

## Compatibility

Within the `0.x` series, patch releases preserve documented CLI and Python API behavior. Minor releases may make incompatible changes with migration notes. Alpha APIs may evolve between prereleases; changes will be recorded in release notes. Only Python versions exercised in CI are claimed as supported.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

Licensed under Apache-2.0. This project is independent of the Apache Software Foundation.
