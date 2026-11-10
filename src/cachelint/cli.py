"""Command-line entry point for cachelint."""

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from cachelint import (
    ComparisonResult,
    CorpusLimits,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    __version__,
    analyze_corpus,
    compare_requests,
)
from cachelint._ingest import read_json, read_jsonl
from cachelint.report import render_corpus_json, render_corpus_text


def _provider_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("auto", "anthropic", "openai"),
        default="auto",
        help="request provider (default: auto; specify one for ambiguous bodies)",
    )
    parser.add_argument(
        "--estimate-method",
        choices=("heuristic", "tiktoken"),
        default="heuristic",
        help="prefix-length method (default: heuristic; tiktoken optional for OpenAI)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="cachelint",
        description="Offline diagnostics for LLM prompt-cache requests.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command")
    analyze = commands.add_parser(
        "analyze", help="compare all request bodies in a UTF-8 JSONL file"
    )
    analyze.add_argument("path", help="file with one raw request object per line")
    _provider_option(analyze)
    diff = commands.add_parser("diff", help="compare two UTF-8 JSON request body files")
    diff.add_argument("left", help="first raw request object")
    diff.add_argument("right", help="second raw request object")
    _provider_option(diff)
    corpus = commands.add_parser(
        "corpus", help="analyze bounded JSONL files or directories as one corpus"
    )
    corpus.add_argument("paths", nargs="+", help="JSONL files or directories")
    _provider_option(corpus)
    corpus.add_argument(
        "--format", choices=("text", "json"), default="text", help="report format"
    )
    corpus.add_argument(
        "--output",
        metavar="PATH",
        help="atomically replace a report file (default: stdout)",
    )
    defaults = CorpusLimits()
    for name, default in (
        ("max-files", defaults.max_files),
        ("max-bytes", defaults.max_bytes),
        ("max-requests", defaults.max_requests),
        ("max-entries", defaults.max_entries),
    ):
        corpus.add_argument(f"--{name}", type=int, default=default, metavar="N")
    return parser


def _write_corpus_report(
    destination: str, inputs: Sequence[str], contents: str
) -> None:
    path = Path(destination)
    if path.suffix.lower() == ".jsonl":
        raise InvalidRequestError("report output must not have a JSONL extension")
    if path.is_symlink():
        raise InvalidRequestError("report output must not be a symlink")
    if any(path.resolve() == Path(source).resolve() for source in inputs):
        raise InvalidRequestError("report output must not replace an input")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".cachelint-report-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise InvalidRequestError("cannot write report") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _print_result(result: ComparisonResult) -> None:
    print(f"Provider: {result.provider}")
    print(f"Requests: {result.request_count}")
    print(f"Prompt status: {result.prompt_status}")
    print(f"Common prefix segments: {result.common_prefix_segments}")
    if result.first_divergence is None:
        print("First divergence: none")
    else:
        divergence = result.first_divergence
        offset = "" if divergence.offset is None else f", offset {divergence.offset}"
        print(f"First divergence: {divergence.path} ({divergence.kind}{offset})")
    context = ", ".join(item.category for item in result.context_differences)
    print(f"Context differences: {context or 'none'}")
    warnings = ", ".join(result.coverage_warnings)
    print(f"Coverage warnings: {warnings or 'none'}")
    estimate = result.prefix_estimate
    if estimate is None:
        print("Stable prefix estimate: unavailable (need two requests)")
    else:
        print(
            f"Stable prefix estimate: ~{estimate.tokens} modeled tokens "
            f"({estimate.method}; {estimate.uncertainty})"
        )
        if estimate.minimum_cacheable_tokens is None:
            print("Documented model minimum: unknown")
        else:
            print(
                "Documented model minimum: "
                f"{estimate.minimum_cacheable_tokens} tokens "
                f"(reviewed {estimate.rules_reviewed_on})"
            )
            print(
                "Estimate vs minimum: "
                + (
                    "at or above"
                    if estimate.minimum_comparison == "estimate_at_or_above"
                    else "below"
                )
                + " (advisory only)"
            )
            print(f"Minimum source: {estimate.minimum_source}")
    print("Provider-reported cached usage: unavailable (request bodies only)")
    print("Findings:")
    for finding in result.findings:
        print(f"  - {finding.code}: {finding.message}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run an offline comparison and return a documented exit status."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        return 0
    provider = cast(Provider | None, None if args.provider == "auto" else args.provider)
    try:
        if args.command == "corpus":
            corpus_result = analyze_corpus(
                args.paths,
                provider=provider,
                estimate_method=args.estimate_method,
                limits=CorpusLimits(
                    max_files=args.max_files,
                    max_bytes=args.max_bytes,
                    max_requests=args.max_requests,
                    max_entries=args.max_entries,
                ),
            )
            rendered = (
                render_corpus_json(corpus_result)
                if args.format == "json"
                else render_corpus_text(corpus_result)
            )
            if args.output is None:
                print(rendered, end="")
            else:
                _write_corpus_report(args.output, args.paths, rendered)
            return 0
        if args.command == "analyze":
            result = compare_requests(
                read_jsonl(args.path, provider=provider),
                estimate_method=args.estimate_method,
            )
        else:
            result = compare_requests(
                [
                    read_json(args.left, provider=provider),
                    read_json(args.right, provider=provider),
                ],
                estimate_method=args.estimate_method,
            )
        _print_result(result)
    except (InvalidRequestError, IncomparableRequestsError) as error:
        print(f"cachelint: error: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("cachelint: error: internal failure", file=sys.stderr)
        return 1
    return 0
