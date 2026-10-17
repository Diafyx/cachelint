"""Command-line entry point for cachelint."""

import argparse
import sys
from collections.abc import Sequence
from typing import cast

from cachelint import (
    ComparisonResult,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    __version__,
    compare_requests,
)
from cachelint._ingest import read_json, read_jsonl


def _provider_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("auto", "anthropic", "openai"),
        default="auto",
        help="request provider (default: auto; specify one for ambiguous bodies)",
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
    return parser


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
        if args.command == "analyze":
            result = compare_requests(read_jsonl(args.path, provider=provider))
        else:
            result = compare_requests(
                [
                    read_json(args.left, provider=provider),
                    read_json(args.right, provider=provider),
                ]
            )
        _print_result(result)
    except (InvalidRequestError, IncomparableRequestsError) as error:
        print(f"cachelint: error: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("cachelint: error: internal failure", file=sys.stderr)
        return 1
    return 0
