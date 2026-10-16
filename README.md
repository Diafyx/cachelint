# cachelint

cachelint is a Python package for diagnosing why LLM requests miss provider prompt caches. Its planned first useful release will compare request bodies offline and show where the stable prompt prefix changes.

**Status:** pre-release. The current command-line interface supports `--help` and `--version` only. Request analysis, SDK recording, and PyPI publication are not available yet.

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

## Planned first analysis release

The `0.1.0a1` milestone targets offline JSONL analysis for Anthropic Messages and OpenAI Chat Completions request bodies. It will locate the first stable-prefix divergence and report actionable findings through `analyze` and `diff`. The canonicalization contract and privacy handling must be reviewed before those commands are implemented. A matching text prefix is evidence for diagnosis, not a guarantee of a provider cache hit.

The proposed [request comparison and privacy contract](docs/design/0001-request-comparison-contract.md) defines the first release's input and diagnostic behavior for review.

Request logs can contain prompts, credentials, personal data, and customer content. Do not commit raw logs or paste them into public issues.

## Compatibility

Within the `0.x` series, patch releases preserve documented CLI and Python API behavior. Minor releases may make incompatible changes with migration notes. Alpha APIs may evolve between prereleases; changes will be recorded in release notes. Only Python versions exercised in CI are claimed as supported.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

Licensed under Apache-2.0. This project is independent of the Apache Software Foundation.
