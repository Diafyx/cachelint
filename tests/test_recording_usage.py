"""Sidecar format, privacy, and failure tests without provider network calls."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import warnings
from importlib.resources import files
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx2
import pytest

from cachelint import recording
from cachelint._usage import MAX_USAGE_RESPONSE_BYTES, UsageBuffer, parse_usage
from cachelint.recording import (
    AsyncRequestRecorder,
    RecorderSetupError,
    RecorderWarning,
    RequestRecorder,
)


class HookClient:
    def __init__(self, *, event_hooks: dict[str, list[Any]]) -> None:
        self.request = event_hooks["request"][0]
        self.response = event_hooks.get("response", [None])[0]


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = ModuleType("fake_usage_sdk")
    sdk.DefaultHttpxClient = HookClient  # type: ignore[attr-defined]
    sdk.DefaultHttpx2Client = HookClient  # type: ignore[attr-defined]
    sdk.DefaultAsyncHttpxClient = HookClient  # type: ignore[attr-defined]
    monkeypatch.setattr(RequestRecorder, "_load_sdk", lambda self: sdk)
    monkeypatch.setattr(AsyncRequestRecorder, "_load_sdk", lambda self: sdk)


def new_recorder(tmp_path: Path, *, capture_usage: bool = True) -> RequestRecorder:
    return RequestRecorder(
        tmp_path / "requests.jsonl",
        provider="anthropic",
        capture_usage=capture_usage,
        allow_windows_acl_inheritance=True,
    )


def request() -> httpx2.Request:
    return httpx2.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        json={"model": "demo", "messages": []},
    )


def response(req: httpx2.Request, body: object) -> httpx2.Response:
    return httpx2.Response(200, request=req, json=body)


def rows(tmp_path: Path) -> list[dict[str, object]]:
    sidecar = tmp_path / "requests.jsonl.usage.jsonl"
    return [json.loads(line) for line in sidecar.read_text().splitlines()]


def test_sidecar_is_opt_in_and_existing_request_file_stays_raw(tmp_path: Path) -> None:
    req = request()
    with new_recorder(tmp_path, capture_usage=False) as recorder:
        client = recorder.http_client()
        client.request(req)
        assert client.response is None
    assert not (tmp_path / "requests.jsonl.usage.jsonl").exists()
    with new_recorder(tmp_path) as recorder:
        client = recorder.http_client()
        client.request(req)
        client.response(response(req, {"usage": {"input_tokens": 3}}))
    assert recorder.status.written == 1
    assert recorder.status.usage_written == 1
    assert rows(tmp_path)[0]["usage"] == {"input_tokens": 3}
    first_line = (tmp_path / "requests.jsonl").read_bytes().splitlines(keepends=True)[0]
    assert rows(tmp_path)[0]["request_offset"] == len(first_line)
    assert (tmp_path / "requests.jsonl").read_bytes().count(b"\n") == 2
    with pytest.raises(RecorderSetupError, match="unsafe"):
        new_recorder(tmp_path).__enter__()


def test_unmatched_attempts_and_response_are_never_reassigned(tmp_path: Path) -> None:
    first, second = request(), request()
    with new_recorder(tmp_path) as recorder:
        client = recorder.http_client()
        client.request(first)
        client.response(response(second, {"usage": {"input_tokens": 8}}))
    actual = rows(tmp_path)
    assert {row["attempt_id"] for row in actual} == {1, 2}
    orphan = next(row for row in actual if row["attempt_id"] == 2)
    assert orphan["capture_status"] == "unmatched"
    assert orphan["usage"] == {"input_tokens": 8}
    original = next(row for row in actual if row["attempt_id"] == 1)
    assert original["capture_status"] == "written"
    assert original["usage_status"] == "unmatched"


def test_pending_bound_emits_explicit_unmatched_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "_MAX_PENDING_USAGE", 2)
    with new_recorder(tmp_path) as recorder:
        client = recorder.http_client()
        for _ in range(3):
            client.request(request())
    assert [row["attempt_id"] for row in rows(tmp_path)] == [1, 2, 3]
    assert {row["usage_status"] for row in rows(tmp_path)} == {"unmatched"}
    assert recorder.status.usage_written == 3


def test_bounded_response_capture_preserves_original_bytes(tmp_path: Path) -> None:
    req = request()
    raw = (
        b'{"usage":{"input_tokens":4},"private":"'
        + b"x" * MAX_USAGE_RESPONSE_BYTES
        + b'"}'
    )
    with new_recorder(tmp_path) as recorder:
        client = recorder.http_client()
        client.request(req)
        provider_response = httpx2.Response(
            200, request=req, stream=httpx2.ByteStream(raw)
        )
        client.response(provider_response)
        assert provider_response.read() == raw
    assert rows(tmp_path)[0]["usage_status"] == "too_large"
    assert b"private" not in (tmp_path / "requests.jsonl.usage.jsonl").read_bytes()


def test_incomplete_response_is_explicit_and_original_error_survives(
    tmp_path: Path,
) -> None:
    class BrokenStream(httpx2.SyncByteStream):
        def __iter__(self) -> Any:
            yield b'{"usage":'
            raise RuntimeError("private stream error")

    req = request()
    with new_recorder(tmp_path) as recorder:
        client = recorder.http_client()
        client.request(req)
        provider_response = httpx2.Response(200, request=req, stream=BrokenStream())
        client.response(provider_response)
        with pytest.raises(RuntimeError, match="private stream error"):
            provider_response.read()
    assert rows(tmp_path)[0]["usage_status"] == "incomplete"
    assert (
        b"private stream error"
        not in (tmp_path / "requests.jsonl.usage.jsonl").read_bytes()
    )


def test_sidecar_short_write_does_not_change_request_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RecorderWarning)
        with new_recorder(tmp_path) as recorder:
            sidecar_fd = recorder._usage_fd
            assert sidecar_fd is not None

            def short_write(fd: int, data: bytes) -> int:
                if fd == sidecar_fd:
                    return original_write(fd, data[:3])
                return original_write(fd, data)

            monkeypatch.setattr(os, "write", short_write)
            client = recorder.http_client()
            first, second = request(), request()
            client.request(first)
            client.response(response(first, {"usage": {"input_tokens": 4}}))
            client.request(second)
            client.response(response(second, {"usage": {"input_tokens": 5}}))
    assert recorder.status.written == 2
    assert recorder.status.usage_failed == 2
    assert recorder.status.usage_written == 0
    assert [str(item.message) for item in caught] == ["usage_write_failed"]
    assert (tmp_path / "requests.jsonl.usage.jsonl").read_bytes() == b""
    assert (tmp_path / "requests.jsonl").read_bytes().count(b"\n") == 2


def test_sidecar_open_failure_releases_raw_file_and_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RequestRecorder._open_destination

    def broken(path: Path, *, exclusive: bool = False) -> int:
        if path.name.endswith(".usage.jsonl"):
            raise OSError("private path")
        return original(path, exclusive=exclusive)

    monkeypatch.setattr(RequestRecorder, "_open_destination", staticmethod(broken))
    with pytest.raises(RecorderSetupError, match="unsafe"):
        new_recorder(tmp_path).__enter__()
    assert not (tmp_path / "requests.jsonl.lock").exists()


def test_sidecar_write_error_and_warning_filter_do_not_affect_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_lseek = os.lseek

    with warnings.catch_warnings():
        warnings.simplefilter("error", RecorderWarning)
        with new_recorder(tmp_path) as recorder:
            sidecar_fd = recorder._usage_fd
            assert sidecar_fd is not None

            def broken_lseek(fd: int, offset: int, how: int) -> int:
                if fd == sidecar_fd:
                    raise OSError("private path")
                return original_lseek(fd, offset, how)

            monkeypatch.setattr(os, "lseek", broken_lseek)
            client = recorder.http_client()
            req = request()
            client.request(req)
            client.response(response(req, {"usage": {"input_tokens": 5}}))
    assert recorder.status.written == 1
    assert recorder.status.usage_failed == 1
    assert (tmp_path / "requests.jsonl.usage.jsonl").read_bytes() == b""


def test_sidecar_failed_truncation_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    original_truncate = os.ftruncate
    with pytest.warns(RecorderWarning, match="usage_write_failed"):
        with new_recorder(tmp_path) as recorder:
            sidecar_fd = recorder._usage_fd
            assert sidecar_fd is not None

            def short_write(fd: int, data: bytes) -> int:
                return original_write(fd, data[:3] if fd == sidecar_fd else data)

            def broken_truncate(fd: int, length: int) -> None:
                if fd == sidecar_fd:
                    raise OSError("private path")
                original_truncate(fd, length)

            monkeypatch.setattr(os, "write", short_write)
            monkeypatch.setattr(os, "ftruncate", broken_truncate)
            client = recorder.http_client()
            req = request()
            client.request(req)
            client.response(response(req, {"usage": {"input_tokens": 5}}))
    assert recorder.status.usage_failed == 1
    assert (tmp_path / "requests.jsonl.usage.jsonl").read_bytes() == b'{"s'


def test_unsupported_destination_and_duplicate_hook_never_reassign_usage(
    tmp_path: Path,
) -> None:
    req = request()
    unsupported = httpx2.Request("GET", "https://api.anthropic.com/v1/messages")
    with pytest.warns(RecorderWarning, match="unsupported_destination"):
        with new_recorder(tmp_path) as recorder:
            client = recorder.http_client()
            client.request(unsupported)
            client.response(response(unsupported, {"usage": {"input_tokens": 1}}))
            client.request(req)
            client.request(req)
            client.response(response(req, {"usage": {"input_tokens": 2}}))
    assert [row["attempt_id"] for row in rows(tmp_path)] == [1, 2]
    assert [row["usage_status"] for row in rows(tmp_path)] == [
        "unmatched",
        "reported",
    ]
    assert rows(tmp_path)[1]["usage"] == {"input_tokens": 2}


def test_unexpected_empty_capture_and_stale_completion_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = request()
    with pytest.warns(RecorderWarning, match="invalid_body"):
        with new_recorder(tmp_path) as recorder:
            monkeypatch.setattr(recorder, "_prepare", lambda _request: (None, None))
            client = recorder.http_client()
            client.request(req)
            client.response(response(req, {"usage": {"input_tokens": 5}}))
            recorder._finish_usage(req, 1, "reported", {"input_tokens": 99})
    assert len(rows(tmp_path)) == 1
    assert rows(tmp_path)[0]["capture_status"] == "skipped"
    assert rows(tmp_path)[0]["capture_code"] == "invalid_body"
    assert rows(tmp_path)[0]["usage"] == {"input_tokens": 5}


def test_unusable_response_metadata_is_explicit_or_ignored(tmp_path: Path) -> None:
    req = request()

    class NoStatus:
        request = req

        @property
        def status_code(self) -> int:
            raise RuntimeError("private response detail")

    class NoIterator:
        request = req
        status_code = 200
        headers: dict[str, str] = {}

        @property
        def content(self) -> bytes:
            raise RuntimeError("not read")

        @property
        def iter_bytes(self) -> Any:
            raise RuntimeError("private stream detail")

    with new_recorder(tmp_path) as recorder:
        client = recorder.http_client()
        client.response(SimpleNamespace(status_code=200))
        client.response(
            response(httpx2.Request("GET", "https://api.anthropic.com/v1/messages"), {})
        )
        client.request(req)
        client.response(NoStatus())
        client.request(req)
        client.response(NoIterator())
    assert [row["usage_status"] for row in rows(tmp_path)] == [
        "incomplete",
        "incomplete",
    ]
    assert (
        b"private response detail"
        not in (tmp_path / "requests.jsonl.usage.jsonl").read_bytes()
    )
    client.response(response(req, {"usage": {"input_tokens": 8}}))
    assert len(rows(tmp_path)) == 2


def test_async_hook_failures_do_not_attribute_other_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = request()

    class NoAsyncIterator:
        request = req
        status_code = 200
        headers: dict[str, str] = {}

        @property
        def content(self) -> bytes:
            raise RuntimeError("not read")

        @property
        def aiter_bytes(self) -> Any:
            raise RuntimeError("private stream detail")

    async def scenario() -> AsyncRequestRecorder:
        recorder = AsyncRequestRecorder(
            tmp_path / "requests.jsonl",
            provider="anthropic",
            capture_usage=True,
            allow_windows_acl_inheritance=True,
        )
        async with recorder:
            client = recorder.http_client()
            await client.response(SimpleNamespace(status_code=200))
            await client.request(req)
            await client.response(NoAsyncIterator())
            loop = asyncio.get_running_loop()

            def broken_executor(*_args: object) -> object:
                raise RuntimeError("private executor detail")

            with monkeypatch.context() as patch:
                patch.setattr(loop, "run_in_executor", broken_executor)
                await client.request(request())
        return recorder

    with pytest.warns(RecorderWarning, match="write_failed"):
        recorder = asyncio.run(scenario())
    assert recorder.status.usage_written == 2
    assert [row["usage_status"] for row in rows(tmp_path)] == [
        "incomplete",
        "unmatched",
    ]


def test_async_buffered_response_finishes_without_stream_wrapper(
    tmp_path: Path,
) -> None:
    async def scenario() -> AsyncRequestRecorder:
        async with AsyncRequestRecorder(
            tmp_path / "requests.jsonl",
            provider="anthropic",
            capture_usage=True,
            allow_windows_acl_inheritance=True,
        ) as recorder:
            client = recorder.http_client()
            req = request()
            await client.request(req)
            await client.response(response(req, {"usage": {"input_tokens": 6}}))
        return recorder

    recorder = asyncio.run(scenario())
    assert recorder.status.usage_written == 1
    assert rows(tmp_path)[0]["usage"] == {"input_tokens": 6}


def test_sidecar_setup_rejects_symlink_and_permissive_file(tmp_path: Path) -> None:
    sidecar = tmp_path / "requests.jsonl.usage.jsonl"
    target = tmp_path / "target"
    target.write_text("private", encoding="utf-8")
    try:
        sidecar.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RecorderSetupError, match="unsafe"):
        new_recorder(tmp_path).__enter__()
    assert target.read_text(encoding="utf-8") == "private"
    sidecar.unlink()
    if os.name != "nt":
        sidecar.write_text("", encoding="utf-8")
        sidecar.chmod(0o644)
        with pytest.raises(RecorderSetupError, match="unsafe"):
            new_recorder(tmp_path).__enter__()
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("provider", "body", "expected"),
    [
        (
            "anthropic",
            b'{"usage":{"cache_read_input_tokens":0}}',
            ("reported", {"cache_read_input_tokens": 0}),
        ),
        ("anthropic", b'{"usage":{"input_tokens":true}}', ("invalid", {})),
        ("anthropic", b'{"usage":{"input_tokens":-1}}', ("invalid", {})),
        ("anthropic", b'{"usage":{"input_tokens":1.5}}', ("invalid", {})),
        (
            "anthropic",
            b'{"usage":{"input_tokens":1,"input_tokens":2}}',
            ("invalid", {}),
        ),
        ("anthropic", b'{"usage":{"input_tokens":NaN}}', ("invalid", {})),
        ("anthropic", b'{"usage":"private"}', ("invalid", {})),
        ("anthropic", b'{"usage":{"unknown":9}}', ("absent", {})),
        ("anthropic", b"not json", ("invalid", {})),
        ("anthropic", b"\xff", ("invalid", {})),
        ("anthropic", b"[]", ("invalid", {})),
        (
            "openai",
            b'{"usage":{"prompt_tokens_details":{"cached_tokens":5}}}',
            ("reported", {"cached_tokens": 5}),
        ),
        (
            "openai",
            b'{"usage":{"prompt_tokens_details":{"cached_tokens":false}}}',
            ("invalid", {}),
        ),
        ("openai", b'{"usage":{"prompt_tokens":-1}}', ("invalid", {})),
        ("openai", b'{"usage":{"prompt_tokens_details":[]}}', ("invalid", {})),
        ("openai", b'{"usage":{"prompt_tokens_details":{}}}', ("absent", {})),
        (
            "openai",
            b'{"usage":{"prompt_tokens":4,"total_tokens":6}}',
            ("reported", {"prompt_tokens": 4, "total_tokens": 6}),
        ),
    ],
)
def test_only_allowlisted_valid_counters_are_returned(
    provider: Any, body: bytes, expected: tuple[str, dict[str, int]]
) -> None:
    assert parse_usage(provider, body) == expected


def test_usage_buffer_discards_oversized_content() -> None:
    buffer = UsageBuffer()
    buffer.add(b"x" * MAX_USAGE_RESPONSE_BYTES)
    buffer.add(b"x")
    buffer.add(b"still ignored")
    assert buffer.result("anthropic") == ("too_large", {})
    assert parse_usage("anthropic", b"x" * (MAX_USAGE_RESPONSE_BYTES + 1)) == (
        "too_large",
        {},
    )


def test_packaged_usage_schema_declares_strict_row_contract() -> None:
    schema = json.loads(
        files("cachelint").joinpath("usage_sidecar_v1.schema.json").read_text()
    )
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["usage"]["additionalProperties"] is False
    assert "request_offset" in schema["properties"]
