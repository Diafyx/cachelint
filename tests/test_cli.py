"""Tests for the public command-line entry point."""

from importlib.metadata import version

import pytest

from cachelint import __version__
from cachelint.cli import main


def test_runtime_version_matches_distribution_metadata() -> None:
    assert __version__ == version("cachelint")


def test_no_arguments_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr() == ("", "")


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "analysis commands are under development" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out == f"cachelint {__version__}\n"


def test_unknown_argument(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--unknown"])
    assert error.value.code == 2
    assert "unrecognized arguments: --unknown" in capsys.readouterr().err
