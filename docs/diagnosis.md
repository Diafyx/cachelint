# Diagnose recurring prefix drift

The `diagnose` command classifies supported differences within Anthropic
Messages and OpenAI Chat Completions cohorts. It runs offline on private JSONL
request bodies. The command and report are included in 0.3.0a1.

~~~console
cachelint diagnose examples/diagnosis/anthropic.jsonl examples/diagnosis/openai.jsonl
cachelint diagnose examples/diagnosis/anthropic.jsonl examples/diagnosis/openai.jsonl --format json --output diagnosis-report.json
~~~

Each file can contain one or more requests. A directory input is searched
recursively for `.jsonl` files, excluding `.usage.jsonl` sidecars. Files are
read in normalized absolute-path order and physical line order. Requests are
grouped by provider and exact model; the model name is omitted from results.
Use `--provider anthropic` or `--provider openai` when a body is ambiguous.
An empty file yields a report with zero cohorts; an invalid JSONL line fails
the whole command. The Python entry point uses the same ordering and report:

~~~python
from cachelint import analyze_diagnosis, render_diagnosis_json

result = analyze_diagnosis(
    ["examples/diagnosis/anthropic.jsonl", "examples/diagnosis/openai.jsonl"]
)
print(render_diagnosis_json(result), end="")
~~~

For the same inputs and options, a JSON `--output` file is byte-for-byte the
UTF-8 encoding of `render_diagnosis_json(result)`. A text `--output` file
likewise matches `render_diagnosis_text(result)`. Both end with one newline;
terminal stdout may apply its host's newline handling. The
[bundled diagnosis schema v1](../src/cachelint/diagnosis_report_v1.schema.json)
uses `schema_version: 1` and `report_type: cachelint.diagnosis`. Findings
include rank, cohort index, provider, fixed code, evidence level, safe path,
count, fixed explanation, and fixed action. The schema is separate from corpus
report v1/v2 and baseline v1; those meanings and readers remain unchanged.

The default corpus limits are 128 files, 16 MiB of input, 20,000 requests, and
4,096 discovery entries. Each cohort is also limited to 200,000 canonical
segments, 2 million nodes, 16,777,216 text characters, and 100 million
approximate comparison work units.
The command exposes matching `--max-*` flags; Python callers can pass
`CorpusLimits` and `DiagnosisLimits`. The command exits 0 on a valid report,
2 for invalid input or output, and 1 for an unexpected internal failure.
`--output` writes a temporary file in the destination directory and atomically
replaces the report only after successful analysis. It refuses symlinks,
`.jsonl` destinations, and direct input paths.
Diagnose errors report fixed reasons and physical line numbers without printing
source paths or request values; keep the private source files protected.

## From private recording to a safe report

1. Create a private recording directory, then use the pinned SDK hooks in the
   [recording guide](recording.md) to collect Anthropic Messages and OpenAI
   Chat Completions requests into separate JSONL files. Close each recorder
   before analysis. The two files and optional usage sidecars remain private.
2. Run `cachelint diagnose /private/anthropic-requests.jsonl /private/openai-requests.jsonl --format json --output /private/diagnosis-report.json`.
3. Review the report's safe paths and counts, then inspect source requests
   privately before changing prompt construction. The report carries no
   source paths, model names, request values, excerpts, or content hashes.

For an offline package smoke test, build both distributions, install the wheel
in one clean environment and the sdist in another, and run the same example
against each installation:

~~~console
python -m build --wheel --sdist
python -m pip install path/to/cachelint-<version>-py3-none-any.whl
python -I examples/diagnosis/smoke.py
python -m pip install path/to/cachelint-<version>.tar.gz
python -I examples/diagnosis/smoke.py
~~~

Use separate environments for the wheel and sdist checks. The smoke program
uses only bundled synthetic requests and makes no provider API calls.

## Python cohort classifier

`diagnose_requests` classifies one already-canonicalized provider/model cohort:

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

All requests in this call must use the same provider and exact model. The first
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
