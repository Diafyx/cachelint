"""Command-line entry point for cachelint."""

import argparse
from collections.abc import Sequence

from cachelint import __version__


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="cachelint",
        description=(
            "Prompt-cache diagnostics; analysis commands are under development."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the current command-line interface."""
    build_parser().parse_args(argv)
    return 0
