"""Exercise installed observed-usage APIs and CLI on synthetic private traces."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import cast

from cachelint import CorpusUsageTrace, Provider, analyze_observed_corpus
from cachelint.baseline import baseline_from_report, render_baseline_json
from cachelint.cli import main
from cachelint.report import corpus_report

CANARY = "PRIVATE_RELEASE_SMOKE_CANARY"


def _recording(
    root: Path, provider: Provider, prompts: tuple[str, ...], reads: tuple[int, ...]
) -> CorpusUsageTrace:
    requests = root / f"{provider}.jsonl"
    sidecar = root / f"{provider}.jsonl.usage.jsonl"
    request_data = b""
    sidecar_rows: list[dict[str, object]] = []
    for attempt, (prompt, read) in enumerate(zip(prompts, reads, strict=True), 1):
        body: dict[str, object] = {
            "model": "claude-sonnet-4-6" if provider == "anthropic" else "gpt-5.6-sol",
            "messages": [{"role": "user", "content": prompt}],
        }
        if provider == "anthropic":
            body["system"] = CANARY
            usage = {"cache_read_input_tokens": read}
        else:
            body["prompt_cache_key"] = CANARY
            usage = {"cached_tokens": read}
        sidecar_rows.append(
            {
                "schema_version": 1,
                "attempt_id": attempt,
                "capture_status": "written",
                "request_offset": len(request_data),
                "usage_status": "reported",
                "usage": usage,
            }
        )
        request_data += (json.dumps(body) + "\n").encode("utf-8")
    requests.write_bytes(request_data)
    sidecar.write_bytes(
        b"".join((json.dumps(row) + "\n").encode("utf-8") for row in sidecar_rows)
    )
    return CorpusUsageTrace(provider, requests, sidecar)


def main_smoke() -> None:
    """Check both providers, v1 compatibility, v2 findings, and privacy."""
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
        root = Path(folder)
        anthropic = _recording(root, "anthropic", (CANARY + "A", CANARY + "B"), (0, 0))
        openai = _recording(
            root, "openai", (CANARY + "A", CANARY + "A", CANARY + "B"), (0, 0, 7)
        )
        result = analyze_observed_corpus(root, usage_traces=[anthropic, openai])
        document = corpus_report(result)
        assert document["schema_version"] == 2
        baseline = baseline_from_report(document)
        assert baseline["source_report_schema_version"] == 2
        assert baseline["summary"]["observed_finding_count"] > 0
        assert CANARY not in render_baseline_json(baseline)
        summary = cast(dict[str, object], document["summary"])
        assert summary["traced_request_count"] == 5
        findings = cast(list[dict[str, object]], document["observed_findings"])
        codes = {item["code"] for item in findings}
        assert {
            "matching_prefix_no_read",
            "divergent_prefix_no_read",
            "read_with_divergence",
        } <= codes
        assert CANARY not in json.dumps(document)
        v2_output = root / "observed.json"
        assert (
            main(
                [
                    "corpus",
                    str(root),
                    "--format",
                    "json",
                    "--output",
                    str(v2_output),
                    "--usage-trace",
                    "anthropic",
                    str(anthropic.requests),
                    str(anthropic.sidecar),
                    "--usage-trace",
                    "openai",
                    str(openai.requests),
                    str(openai.sidecar),
                ]
            )
            == 0
        )
        assert json.loads(v2_output.read_text(encoding="utf-8")) == document
        v1_output = root / "request-only.json"
        assert (
            main(["corpus", str(root), "--format", "json", "--output", str(v1_output)])
            == 0
        )
        assert json.loads(v1_output.read_text(encoding="utf-8"))["schema_version"] == 1
        for trace in (anthropic, openai):
            usage_output = root / f"{trace.provider}-usage.json"
            assert (
                main(
                    [
                        "usage",
                        "--provider",
                        trace.provider,
                        "--trace",
                        str(trace.requests),
                        str(trace.sidecar),
                        "--format",
                        "json",
                        "--output",
                        str(usage_output),
                    ]
                )
                == 0
            )
            report = usage_output.read_text(encoding="utf-8")
            assert CANARY not in report
            assert json.loads(report)["schema_version"] == 1
    print("Installed observed-usage smoke passed for Anthropic and OpenAI")


if __name__ == "__main__":
    main_smoke()
