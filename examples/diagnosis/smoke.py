"""Offline installed-package smoke test for the two-provider diagnosis example."""

from __future__ import annotations

import json
from pathlib import Path

from cachelint import analyze_diagnosis, render_diagnosis_json
from cachelint.cli import main

root = Path(__file__).resolve().parent
paths = [root / "anthropic.jsonl", root / "openai.jsonl"]
result = analyze_diagnosis(paths)
report = json.loads(render_diagnosis_json(result))
assert report["schema_version"] == 1
assert report["summary"]["request_count"] == 4
assert [(item["code"], item["provider"]) for item in report["findings"]] == [
    ("volatile_timestamp", "anthropic"),
    ("volatile_identifier", "openai"),
]
assert main(["diagnose", *(str(path) for path in paths), "--format", "json"]) == 0
