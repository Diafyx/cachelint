"""Corpus inputs remain bounded and results never retain request values."""

from __future__ import annotations

import json
import os
import stat
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from cachelint import (
    CorpusLimits,
    InvalidRequestError,
    SourceLocation,
    analyze_corpus,
    canonicalize_request,
    compare_requests,
)


def _body(provider: str, model: str, system: str) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "private prompt"}],
    }
    if provider == "anthropic":
        body["system"] = system
    else:
        body["prompt_cache_key"] = system
    return body


def _write(path: Path, *bodies: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(body) + "\n" for body in bodies), encoding="utf-8"
    )
    return path


def test_mixed_files_group_by_provider_and_exact_model(tmp_path: Path) -> None:
    alpha = _write(
        tmp_path / "b.jsonl",
        _body("anthropic", "private-model-two", "B"),
        _body("openai", "private-model-three", "key"),
    )
    beta = _write(
        tmp_path / "a.jsonl",
        _body("anthropic", "private-model-one", "A"),
        _body("anthropic", "private-model-one", "B"),
        _body("anthropic", "private-model-two", "A"),
    )
    result = analyze_corpus([alpha, beta])
    assert result.file_count == 2
    assert result.request_count == 5
    assert result.byte_count == alpha.stat().st_size + beta.stat().st_size
    assert [
        (item.index, item.provider, item.file_count, item.request_count)
        for item in result.groups
    ] == [
        (1, "anthropic", 1, 2),
        (2, "anthropic", 2, 2),
        (3, "openai", 1, 1),
    ]
    assert result.groups[0].comparison.prompt_status == "diverged"
    assert result.groups[1].comparison.prompt_status == "diverged"
    assert result.groups[2].comparison.prompt_status == "insufficient_data"
    assert result.groups[2].comparison.prefix_estimate is None
    assert result == analyze_corpus([beta, alpha])
    for secret in (
        "private prompt",
        "private-model-one",
        "private-model-two",
        "private-model-three",
    ):
        assert secret not in repr(result)


def test_single_file_matches_existing_comparison(tmp_path: Path) -> None:
    first = _body("anthropic", "demo", "First")
    second = _body("anthropic", "demo", "Second")
    path = _write(tmp_path / "requests.jsonl", first, second)
    result = analyze_corpus(str(path))
    expected = compare_requests(
        [canonicalize_request(first), canonicalize_request(second)]
    )
    assert result.groups[0].comparison == expected
    assert result.groups[0].file_count == 1


def test_recursive_directory_discovery_and_empty_file(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    _write(root / "z" / "requests.jsonl", _body("anthropic", "demo", "A"))
    _write(root / "a" / "requests.JSONL", _body("anthropic", "demo", "A"))
    (root / "empty.jsonl").write_bytes(b"")
    (root / "a" / "notes.txt").write_text("private ignored", encoding="utf-8")
    result = analyze_corpus([root])
    assert (result.file_count, result.request_count) == (3, 2)
    assert result.groups[0].file_count == 2
    assert result.groups[0].comparison.prompt_status == "identical"
    assert result.byte_count == sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".jsonl"
    )


def test_empty_file_has_explicit_empty_result(tmp_path: Path) -> None:
    path = tmp_path / "empty.log"
    path.write_bytes(b"\n \r\n")
    result = analyze_corpus([path])
    assert result.file_count == 1
    assert result.request_count == 0
    assert result.byte_count == 4
    assert result.groups == ()


def test_no_input_or_no_discovered_jsonl_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequestError, match="at least one corpus path"):
        analyze_corpus([])
    (tmp_path / "readme.txt").write_text("private", encoding="utf-8")
    with pytest.raises(InvalidRequestError, match="no JSONL input files"):
        analyze_corpus([tmp_path])


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (CorpusLimits(max_files=1), "file limit"),
        (CorpusLimits(max_bytes=5), "byte limit"),
        (CorpusLimits(max_requests=1), "request limit"),
        (CorpusLimits(max_entries=1), "discovery limit"),
    ],
)
def test_bounds_fail_without_returning_partial_results(
    tmp_path: Path, limits: CorpusLimits, message: str
) -> None:
    first = _write(tmp_path / "first.jsonl", _body("anthropic", "demo", "secret A"))
    second = _write(tmp_path / "second.jsonl", _body("anthropic", "demo", "secret B"))
    with pytest.raises(InvalidRequestError, match=message) as error:
        analyze_corpus([first, second], limits=limits)
    assert "secret A" not in str(error.value)
    assert "secret B" not in str(error.value)


def test_duplicate_paths_and_symlinks_are_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "requests.jsonl", _body("anthropic", "demo", "A"))
    with pytest.raises(InvalidRequestError, match="duplicate input file"):
        analyze_corpus([path, tmp_path])
    link = tmp_path / "linked.jsonl"
    try:
        link.symlink_to(path)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable on this system")
    with pytest.raises(InvalidRequestError, match="symlink input"):
        analyze_corpus([link])
    with pytest.raises(InvalidRequestError, match="symlink input"):
        analyze_corpus([tmp_path])


def test_bad_line_reports_location_without_content(tmp_path: Path) -> None:
    path = tmp_path / "requests.jsonl"
    path.write_text(
        '\ufeff{"model":"demo","system":"secret","messages":[]}\n'
        " \n"
        '{"model":"demo","system":"secret","messages":[],"model":"other"}\n',
        encoding="utf-8",
    )
    with pytest.raises(InvalidRequestError) as error:
        analyze_corpus([path])
    assert error.value.source == SourceLocation(str(path.resolve()), 3)
    assert error.value.reason == "duplicate JSON object key"
    assert "secret" not in str(error.value)


def test_invalid_utf8_and_invalid_config_have_safe_errors(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b"\xff__CACHELINT_PRIVATE_PAYLOAD_SENTINEL__")
    with pytest.raises(InvalidRequestError, match="invalid UTF-8") as error:
        analyze_corpus([path])
    assert "__CACHELINT_PRIVATE_PAYLOAD_SENTINEL__" not in str(error.value)
    with pytest.raises(InvalidRequestError, match="positive integers"):
        CorpusLimits(max_files=True)
    with pytest.raises(InvalidRequestError, match="unsupported provider"):
        analyze_corpus([path], provider="other")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError, match="unsupported estimate method"):
        analyze_corpus([path], estimate_method="other")  # type: ignore[arg-type]


def test_unreadable_and_nonfile_inputs_are_safe(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequestError, match="unreadable input path"):
        analyze_corpus([tmp_path / "missing.jsonl"])
    with pytest.raises(InvalidRequestError, match="nonempty strings or Paths"):
        analyze_corpus([""])
    with pytest.raises(InvalidRequestError, match="nonempty strings or Paths"):
        analyze_corpus([123])  # type: ignore[list-item]


def test_directory_discovery_limit_is_enforced_inside_a_directory(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "a.jsonl", _body("anthropic", "demo", "A"))
    _write(tmp_path / "b.jsonl", _body("anthropic", "demo", "B"))
    with pytest.raises(InvalidRequestError, match="discovery limit"):
        analyze_corpus([tmp_path], limits=CorpusLimits(max_entries=2))


def test_symlink_checks_fail_closed_when_a_component_is_inaccessible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "requests.jsonl", _body("anthropic", "demo", "A"))
    original = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        if self == path:
            return True
        return original(self)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", fake_is_symlink)
        with pytest.raises(InvalidRequestError, match="symlink input"):
            analyze_corpus([path])

    def inaccessible(self: Path) -> bool:
        if self == path:
            raise OSError("private operating-system detail")
        return original(self)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", inaccessible)
        with pytest.raises(InvalidRequestError, match="unreadable input path") as error:
            analyze_corpus([path])
    assert "private operating-system detail" not in str(error.value)


def test_file_identity_is_rechecked_after_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "requests.jsonl", _body("anthropic", "demo", "A"))
    original_resolve = Path.resolve

    def fail_resolve(self: Path, strict: bool = False) -> Path:
        if self == path:
            raise OSError("private operating-system detail")
        return original_resolve(self, strict=strict)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", fail_resolve)
        with pytest.raises(InvalidRequestError, match="unreadable input file"):
            analyze_corpus([path])

    original_is_regular = stat.S_ISREG
    calls = 0

    def changed_type(mode: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            return False
        return original_is_regular(mode)

    with monkeypatch.context() as patch:
        patch.setattr("cachelint._corpus.stat.S_ISREG", changed_type)
        with pytest.raises(InvalidRequestError, match="regular file"):
            analyze_corpus([path])


def test_special_root_and_directory_entries_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "requests.jsonl", _body("anthropic", "demo", "A"))
    original_stat = Path.stat

    def special_root(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == path:
            return os.stat_result((stat.S_IFIFO, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        return original_stat(self, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", special_root)
        with pytest.raises(InvalidRequestError, match="regular file or directory"):
            analyze_corpus([path])

    fake_symlink = SimpleNamespace(
        path=str(tmp_path / "linked.jsonl"),
        is_symlink=lambda: True,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "cachelint._corpus.os.scandir",
            lambda _: nullcontext([fake_symlink]),
        )
        with pytest.raises(InvalidRequestError, match="symlink input"):
            analyze_corpus([tmp_path])

    def special_entry(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            path=str(tmp_path / name),
            is_symlink=lambda: False,
            is_dir=lambda **_: False,
            is_file=lambda **_: False,
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            "cachelint._corpus.os.scandir",
            lambda _: nullcontext(
                [special_entry("ignored.txt"), special_entry("special.jsonl")]
            ),
        )
        with pytest.raises(InvalidRequestError, match="regular file"):
            analyze_corpus([tmp_path])


def test_io_failures_do_not_expose_operating_system_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "requests.jsonl", _body("anthropic", "demo", "A"))
    with monkeypatch.context() as patch:
        patch.setattr(
            "cachelint._corpus.os.scandir",
            lambda _: (_ for _ in ()).throw(OSError("private scan detail")),
        )
        with pytest.raises(
            InvalidRequestError, match="unreadable input directory"
        ) as error:
            analyze_corpus([tmp_path])
    assert "private scan detail" not in str(error.value)

    def fail_open(self: Path, mode: str = "r") -> object:
        raise OSError("private read detail")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_open)
        with pytest.raises(InvalidRequestError, match="unreadable input file") as error:
            analyze_corpus([path])
    assert "private read detail" not in str(error.value)


def test_invalid_limits_object_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "requests.jsonl", _body("anthropic", "demo", "A"))
    with pytest.raises(InvalidRequestError, match="invalid corpus limits"):
        analyze_corpus([path], limits=object())  # type: ignore[arg-type]
