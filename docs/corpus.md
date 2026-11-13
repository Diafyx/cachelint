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
    [
        Path("/private/recordings/run-one.jsonl"),
        Path("/private/recordings/run-two.jsonl"),
    ],
    limits=CorpusLimits(max_files=64, max_bytes=8 * 1024 * 1024),
)
for group in result.groups:
    print(group.index, group.provider, group.request_count)
    print(group.comparison.prompt_status, group.comparison.first_divergence)
for pattern in result.patterns:
    print(pattern.rank, pattern.group_index, pattern.path, pattern.count)
```

Pass one path, an iterable of paths, or directories. A direct file path may
have any extension except `.usage.jsonl`; a directory is searched recursively
for files ending in `.jsonl` (case-insensitive), excluding recorder sidecars.
Other directory files are ignored. Every chosen
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
Size the limits for your environment and protect source recordings.

## Ranked findings

Each cohort's first request in normalized file path and physical line order is
its fixed reference. Every later request in that cohort is paired with it once.
Recording timestamps and call order across different files are not used.
Duplicate request bodies are retained as separate observations: another copy
of the reference counts as an identical pair, while another copy of a changed
body increases that divergence pattern's count. A recurring pattern is a
shared cohort, complete common-prefix segment count, safe path, and difference
kind. Text offsets may differ within one pattern; `text_offset_min` and
`text_offset_max` give the observed zero-based Unicode code point range.

`CorpusGroup.paired_count` is `request_count - 1`. Its `identical_count` and
`diverged_count` partition those pairs. `context_changed_count` counts pairs
whose request context differs from the reference, including pairs with an
identical modeled prompt. `coverage_limited_count` counts pairs where either
side has a coverage warning. These last two counts may overlap either prompt
category. A one-request cohort has zero pairs and its existing comparison has
`insufficient_data`; it contributes no ranked pattern.

`CorpusResult.patterns` ranks observed modeled divergences across all cohorts
by descending `count`, then ascending `common_prefix_segments` (earlier
complete-segment break first), then cohort index, safe path, and difference
kind. Ranks start at one. Each pattern includes its group index, provider,
fixed `tools`, `system`, or `messages` category, safe path, kind, count,
context-changed and coverage-limited pair counts, and an advisory
`prefix_estimate` at the **earliest observed text offset** (or the complete
segment boundary for structural changes). This estimate is an example of the
modeled prefix, not an aggregate token count or measured provider usage.
The cohort's `comparison` remains the earliest divergence across all its
requests and may differ from the top ranked pattern.

`provider_reported_cache_status` is `unavailable`: request bodies do not
contain trustworthy response usage. Ranked modeled changes are diagnostic
evidence, not measured cache misses. Identical modeled prefixes do not prove
cache hits. Opaque content and unmodeled context can limit conclusions; inspect
the coverage and context counts before prioritizing a pattern. The result
contains no model names, request values, excerpts, source paths, or persistent
content hashes. Errors can still contain local paths as described above.
For recordings with an opt-in usage sidecar, use the separate
[`analyze_usage` and `cachelint usage` workflow](usage.md) to validate and
summarize provider observations. To combine observations with modeled corpus
patterns, use `analyze_observed_corpus` below. The corpus report version 1
remains a request-only report.

## Join opt-in usage observations

```powershell
cachelint corpus recordings --usage-trace anthropic recordings/run.jsonl recordings/run.jsonl.usage.jsonl --format json --output observed-report.json
```

Repeat `--usage-trace PROVIDER REQUESTS SIDECAR` for each same-session pair.
The request file must belong to the corpus and contain only that provider.
`UsageLimits` bounds the combined usage traces across both providers; pass
`usage_limits=` to the Python API to adjust its defaults.
The command uses [report schema v2](../src/cachelint/corpus_report_v2.schema.json)
when traces are supplied. Recursive directory discovery skips recorder
`.usage.jsonl` sidecars. It rejects a sidecar passed directly as a request
corpus path. The existing `cachelint usage` command remains available for a
standalone, more detailed observation report.

```python
from cachelint import CorpusUsageTrace, analyze_observed_corpus
from cachelint.report import corpus_report

result = analyze_observed_corpus(
    "recordings",
    usage_traces=[
        CorpusUsageTrace(
            "anthropic", "recordings/run.jsonl", "recordings/run.jsonl.usage.jsonl"
        )
    ],
)
document = corpus_report(result)
assert document["schema_version"] == 2
```

Version 2 adds `summary` trace/row/request/finding counts, a `usage` summary
to each cohort, and ranked `observed_findings`. A missing trace, missing
sidecar row, or unusable provider counter counts as unknown, never as a miss.
Read and write positive, zero, and unknown counts are separate. Known token
totals are partial if unknown counts are nonzero; OpenAI writes are unknown
under the pinned recorder contract. Findings can overlap, and their counts
refer to comparisons with the cohort's first request. Observed reads do not
identify reused bytes or a private cache key. Local prefix estimates and
bundled model thresholds are advisory. See [ADR 0006](design/0006-observed-corpus-findings.md)
for ranking, code meanings, privacy, and interpretation limits.

Pairing and aggregation make one pass over the nonreference requests per
cohort and never enumerate all request pairs. Corpus input remains subject to
the configured file, byte, request, and discovery bounds.

## Corpus command and report contract

```powershell
cachelint corpus recordings --format text
cachelint corpus recordings --format json --output report.json
cachelint corpus first.jsonl second.jsonl --max-requests 5000 --max-bytes 8388608
```

The command accepts one or more files or directories, with the same provider
selection and estimate methods as `analyze` and `diff`. It writes text to
standard output by default. Use `--format json` for machine-readable output.
`--output PATH` writes either format to a named file: it creates a temporary
file in the destination directory, flushes it, then atomically replaces the
destination after the entire input has been validated and the report rendered.
An existing report is overwritten. The command rejects a symlink destination,
a destination equal to a direct input, and a `.jsonl` destination. The parent
directory must already exist. Keep reports in a protected directory, even
though the report omits request content. Its file permissions follow the host
operating system and destination directory policy.

The bundled [JSON Schema v1](../src/cachelint/corpus_report_v1.schema.json)
defines every field, code, safe-path shape, and allowed value. The top-level
`schema_version` is `1` and `report_type` is `cachelint.corpus`. The stable
`summary` contains file, byte, request, cohort, and pattern counts plus
`provider_reported_cache_status: "unavailable"`. `groups` contains ordered
cohort counts and the existing comparison evidence. `patterns` contains the
ranked observed divergence records. Optional evidence uses JSON `null`, not a
missing field; empty corpora use empty `groups` and `patterns` arrays. JSON
object keys are sorted, arrays retain deterministic cohort/rank order, and
output ends with a newline. Incompatible report changes require a new schema
version and filename.

Use the [baseline guide](baselines.md) to project a validated v1 or v2 corpus
report into a deterministic, reviewable summary. A baseline retains fixed
finding codes and counts while omitting free-form observed finding text.

Python callers can build the same document without the CLI:

```python
from cachelint import analyze_corpus
from cachelint.report import corpus_report

document = corpus_report(analyze_corpus("recordings"))
assert document["schema_version"] == 1
```

A successful report exits `0`, including empty corpora, identical comparisons,
and one-request cohorts with insufficient evidence. Invalid input, invalid
limits, or an unwritable output destination exits `2` and writes no partial
report. Unexpected internal errors exit `1` with a generic message. Errors
can still include local input paths as described above. Existing `analyze` and
`diff` commands retain their current output and exit behavior.
