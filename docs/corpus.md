# Analyze a corpus of request recordings

`analyze_corpus` reads several private JSONL files in one offline operation.
It groups requests by provider and exact model, then applies the existing
`compare_requests` contract to each group. The result exposes numeric cohort
indexes, provider names, counts, and content-safe comparison results. It does
not expose model strings, file paths, prompt values, snippets, or persistent
content hashes. A modeled matching prefix still does not prove a provider cache
hit.

```python
from pathlib import Path

from cachelint import CorpusLimits, analyze_corpus

result = analyze_corpus(
    [Path("/private/recordings/run-one.jsonl"), Path("/private/recordings/run-two.jsonl")],
    limits=CorpusLimits(max_files=64, max_bytes=8 * 1024 * 1024),
)
for group in result.groups:
    print(group.index, group.provider, group.request_count)
    print(group.comparison.prompt_status, group.comparison.first_divergence)
```

Pass one path, an iterable of paths, or directories. A direct file path may
have any extension; a directory is searched recursively for files ending in
`.jsonl` (case-insensitive). Other directory files are ignored. Every chosen
path must be a regular file or directory, and symlinks anywhere in discovered
inputs are rejected. Duplicate normalized paths, including a file listed both
directly and through a directory, are rejected. Files are read in normalized
absolute path order and then physical line order, regardless of argument
order. Cohorts are numbered by provider and exact model order; the model
strings are used only inside the analysis and are omitted from the result.
Adding a new cohort can renumber later cohorts.

Each nonblank physical line must be one UTF-8 raw request object. An initial
UTF-8 BOM is accepted per file. The same parser and canonicalization rules as
[`cachelint analyze`](ingestion.md) apply, but different providers and models
may appear in the same file. Pass `provider="anthropic"` or
`provider="openai"` if any body lacks a provider marker. A file containing no
requests is accepted; if all selected files are empty, `groups` is empty and
`request_count` is zero. No paths or no discovered JSONL files are errors.
One-request cohorts return `insufficient_data` and no prefix estimate.

Default hard limits are 128 files, 16 MiB of input bytes, 20,000 nonblank
requests, and 4,096 discovery entries (including root paths). Supply a
`CorpusLimits` value to lower or raise them. The entire corpus is validated
before a `CorpusResult` is returned. If discovery, decoding, parsing,
canonicalization, or a limit fails, the call raises `InvalidRequestError`
without a partial result. Error text can include an input path and physical
line number, so treat errors and local logs as private. The error never echoes
the failing request body. Files changed concurrently during a scan may fail
validation; use a stable snapshot for reproducible results.

This API keeps canonical request objects in memory until comparison completes.
Size the limits for your environment and protect source recordings. Ranked
cross-cohort findings and CLI report export are planned for later work in the
`0.2.0a1` milestone; this API returns the underlying per-cohort comparisons.
