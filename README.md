# cachelint

cachelint is a Python package for diagnosing why LLM requests miss provider prompt caches. Its planned first useful release will compare request bodies offline and show where the stable prompt prefix changes.

**Status:** pre-release. The Python API can ingest and compare raw request bodies. The command-line interface still supports `--help` and `--version` only; analysis commands, SDK recording, and PyPI publication are not available yet.

## Requirements and current usage

Python 3.11 or newer is required. The initial CI matrix covers Python 3.11 through 3.14.

For development from this checkout:

```console
python -m pip install --upgrade pip
python -m pip install --group dev -e .
cachelint --help
cachelint --version
```

The `cachelint` distribution, `cachelint` import package, and `cachelint` CLI share the same name. The core package has no runtime dependencies.

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

The result contains safe paths, difference kinds, character offsets where available, context categories, coverage warnings, and evidence-based findings. It contains no prompt excerpts or raw values. See the [comparison guide](docs/comparison.md) for result semantics and privacy limits.

## Planned first analysis release

The `0.1.0a1` milestone targets offline JSONL analysis for Anthropic Messages and OpenAI Chat Completions request bodies. It will locate the first stable-prefix divergence and report actionable findings through `analyze` and `diff`. The accepted canonicalization contract and privacy handling guide those commands. A matching text prefix is evidence for diagnosis, not a guarantee of a provider cache hit.

The [request comparison and privacy contract](docs/design/0001-request-comparison-contract.md) defines the first release's input and diagnostic behavior.

Request logs can contain prompts, credentials, personal data, and customer content. Do not commit raw logs or paste them into public issues.

## Compatibility

Within the `0.x` series, patch releases preserve documented CLI and Python API behavior. Minor releases may make incompatible changes with migration notes. Alpha APIs may evolve between prereleases; changes will be recorded in release notes. Only Python versions exercised in CI are claimed as supported.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

Licensed under Apache-2.0. This project is independent of the Apache Software Foundation.
