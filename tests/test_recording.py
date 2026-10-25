"""Synthetic, local tests for the opt-in SDK recorder."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, overload

import pytest

from cachelint import Provider, recording
from cachelint._ingest import read_jsonl
from cachelint.recording import (
    AsyncRequestRecorder,
    RecorderSetupError,
    RecorderWarning,
    RequestRecorder,
    UnsupportedSDKError,
)


class FakeHttpClient:
    def __init__(self, *, event_hooks: dict[str, list[Any]]) -> None:
        self.hook = event_hooks["request"][0]


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("fake_sdk")
    module.DefaultHttpxClient = FakeHttpClient  # type: ignore[attr-defined]
    module.DefaultHttpx2Client = FakeHttpClient  # type: ignore[attr-defined]
    module.DefaultAsyncHttpxClient = FakeHttpClient  # type: ignore[attr-defined]
    module.DefaultAsyncHttpx2Client = FakeHttpClient  # type: ignore[attr-defined]
    monkeypatch.setattr(RequestRecorder, "_load_sdk", lambda self: module)
    monkeypatch.setattr(AsyncRequestRecorder, "_load_sdk", lambda self: module)


def make_request(
    provider: Provider = "anthropic",
    *,
    body: object | None = None,
    raw: bytes | None = None,
    method: str = "POST",
    host: str | None = None,
    path: str | None = None,
    query: bytes = b"",
) -> SimpleNamespace:
    endpoints = {
        "anthropic": ("api.anthropic.com", "/v1/messages"),
        "openai": ("api.openai.com", "/v1/chat/completions"),
    }
    expected_host, expected_path = endpoints[provider]
    if body is None:
        body = {
            "model": "demo",
            "messages": [{"role": "user", "content": "Hello\nworld"}],
        }
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(
            scheme="https",
            host=host or expected_host,
            path=path or expected_path,
            port=None,
            username="",
            password="",
            query=query,
        ),
        content=raw if raw is not None else json.dumps(body).encode(),
        headers={"authorization": "sk-synthetic"},
    )


@overload
def new_recorder(
    tmp_path: Path,
    *,
    provider: Provider = "anthropic",
    asynchronous: Literal[False] = False,
) -> RequestRecorder: ...


@overload
def new_recorder(
    tmp_path: Path, *, provider: Provider = "anthropic", asynchronous: Literal[True]
) -> AsyncRequestRecorder: ...


def new_recorder(
    tmp_path: Path,
    *,
    provider: Provider = "anthropic",
    asynchronous: bool = False,
) -> RequestRecorder | AsyncRequestRecorder:
    cls = AsyncRequestRecorder if asynchronous else RequestRecorder
    return cls(
        tmp_path / "requests.jsonl",
        provider=provider,
        allow_windows_acl_inheritance=True,
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_sync_capture_is_analyzer_compatible_and_request_is_unchanged(
    tmp_path: Path, provider: Provider
) -> None:
    recorder = new_recorder(tmp_path, provider=provider)
    request = make_request(provider)
    original = request.content
    with recorder:
        client = recorder.http_client()
        client.hook(request)
        client.hook(request)
        assert recorder.status.written == 2
        assert recorder.status.skipped == recorder.status.failed == 0
    assert request.content == original
    output = (tmp_path / "requests.jsonl").read_bytes()
    assert output.count(b"\n") == 2
    assert b"authorization" not in output
    assert b"sk-synthetic" not in output
    parsed = read_jsonl(tmp_path / "requests.jsonl", provider=provider)
    assert len(parsed) == 2
    assert parsed[0].model == "demo"
    assert not (tmp_path / "requests.jsonl.lock").exists()


def test_async_calls_append_complete_lines_and_close_waits(tmp_path: Path) -> None:
    async def scenario() -> None:
        recorder = new_recorder(tmp_path, provider="openai", asynchronous=True)
        assert isinstance(recorder, AsyncRequestRecorder)
        async with recorder:
            client = recorder.http_client()
            await asyncio.gather(
                *(client.hook(make_request("openai")) for _ in range(12))
            )
        assert recorder.status.written == 12

    asyncio.run(scenario())
    assert len(read_jsonl(tmp_path / "requests.jsonl", provider="openai")) == 12


def test_sync_threads_write_without_interleaving(tmp_path: Path) -> None:
    recorder = new_recorder(tmp_path)
    assert isinstance(recorder, RequestRecorder)
    with recorder:
        hook = recorder.http_client().hook
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(hook, [make_request() for _ in range(20)]))
    assert recorder.status.written == 20
    assert len(read_jsonl(tmp_path / "requests.jsonl", provider="anthropic")) == 20


@pytest.mark.parametrize(
    ("attempt", "code"),
    [
        (make_request(method="GET"), "unsupported_destination"),
        (make_request(host="example.invalid"), "unsupported_destination"),
        (make_request(query=b"secret=yes"), "unsupported_destination"),
        (
            make_request(
                body={"model": "demo", "messages": [], "nested": {"API_Key": "x"}}
            ),
            "credential_field",
        ),
        (
            make_request(
                body={"model": "demo", "messages": [], "items": [{"x-api-key": "x"}]}
            ),
            "credential_field",
        ),
        (
            make_request(raw=b'{"model":"demo","model":"again","messages":[]}'),
            "invalid_body",
        ),
        (make_request(raw=b'{"model":"demo","messages":[],"x":NaN}'), "invalid_body"),
        (make_request(raw=b'{"model":"demo","messages":"wrong"}'), "invalid_body"),
        (make_request(raw=b"not JSON"), "invalid_body"),
        (make_request(raw=b"[]"), "invalid_body"),
        (make_request(raw=b"\xff"), "invalid_body"),
        (
            make_request(
                body={
                    "model": "demo",
                    "messages": [{"role": "user", "content": "x" * (1024 * 1024)}],
                }
            ),
            "size_limit",
        ),
    ],
)
def test_skips_unsafe_or_ineligible_attempts(
    tmp_path: Path, attempt: SimpleNamespace, code: str
) -> None:
    recorder = new_recorder(tmp_path)
    original = attempt.content
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RecorderWarning)
        with recorder:
            recorder.http_client().hook(attempt)
            recorder.http_client().hook(attempt)
    assert attempt.content == original
    assert recorder.status.skipped == 2
    assert recorder.status.written == recorder.status.failed == 0
    assert recorder.status.last_failure_code == code
    assert [str(item.message) for item in caught] == [code]
    assert (tmp_path / "requests.jsonl").read_bytes() == b""


def test_nonbuffered_body_and_warning_filter_do_not_affect_request(
    tmp_path: Path,
) -> None:
    class StreamingRequest:
        method = "POST"
        url = make_request().url

        @property
        def content(self) -> bytes:
            raise RuntimeError("body not buffered")

    recorder = new_recorder(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RecorderWarning)
        with recorder:
            recorder.http_client().hook(StreamingRequest())
    assert recorder.status.skipped == 1
    assert recorder.status.last_failure_code == "nonbuffered_body"


def test_short_write_disables_recording_without_changing_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = new_recorder(tmp_path)
    request = make_request()
    actual_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        return actual_write(fd, data[:3])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RecorderWarning)
        with recorder:
            monkeypatch.setattr(os, "write", short_write)
            recorder.http_client().hook(request)
            recorder.http_client().hook(request)
    assert request.content
    assert recorder.status.failed == 2
    assert recorder.status.written == 0
    assert [str(item.message) for item in caught] == ["write_failed"]
    assert (tmp_path / "requests.jsonl").read_bytes() == b""


def test_exclusive_lock_and_existing_file_permissions(tmp_path: Path) -> None:
    first = new_recorder(tmp_path)
    second = new_recorder(tmp_path)
    with first, pytest.raises(RecorderSetupError, match="unsafe"):
        second.__enter__()
    assert not (tmp_path / "requests.jsonl.lock").exists()
    if os.name != "nt":
        path = tmp_path / "requests.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o644)
        with pytest.raises(RecorderSetupError, match="unsafe"):
            new_recorder(tmp_path).__enter__()
        assert not (tmp_path / "requests.jsonl.lock").exists()


def test_setup_rejects_unsafe_paths(tmp_path: Path) -> None:
    with pytest.raises(RecorderSetupError, match="invalid recording path"):
        RequestRecorder(object(), provider="anthropic")  # type: ignore[arg-type]
    with pytest.raises(RecorderSetupError, match="absolute"):
        RequestRecorder("relative.jsonl", provider="anthropic")
    with pytest.raises(RecorderSetupError, match="unsupported provider"):
        RequestRecorder(tmp_path / "x", provider="other")  # type: ignore[arg-type]
    with pytest.raises(RecorderSetupError, match="unsafe"):
        RequestRecorder(
            tmp_path / "missing" / "log",
            provider="anthropic",
            allow_windows_acl_inheritance=True,
        ).__enter__()
    with pytest.raises(RecorderSetupError, match="unsafe"):
        RequestRecorder(
            tmp_path, provider="anthropic", allow_windows_acl_inheritance=True
        ).__enter__()
    with pytest.raises(RecorderSetupError, match="not open"):
        new_recorder(tmp_path).http_client()


def test_version_is_checked_before_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(metadata, "version", lambda name: "0.0.0")
    with pytest.raises(UnsupportedSDKError, match="version"):
        new_recorder(tmp_path).__enter__()
    assert not (tmp_path / "requests.jsonl").exists()


def test_missing_sdk_is_rejected_before_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()

    def missing(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", missing)
    with pytest.raises(UnsupportedSDKError, match="not installed"):
        new_recorder(tmp_path).__enter__()
    assert not (tmp_path / "requests.jsonl").exists()


def test_broken_sdk_import_is_rejected_before_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(metadata, "version", lambda _name: "1.8.0")

    def broken_import(_name: str) -> ModuleType:
        raise ImportError

    monkeypatch.setattr(importlib, "import_module", broken_import)
    with pytest.raises(UnsupportedSDKError, match="not installed"):
        new_recorder(tmp_path).__enter__()
    assert not (tmp_path / "requests.jsonl").exists()


def test_windows_requires_explicit_acl_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "_WINDOWS", True)
    with pytest.raises(RecorderSetupError, match="ACL"):
        RequestRecorder(tmp_path / "requests.jsonl", provider="anthropic").__enter__()


def test_reopen_and_client_factory_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = new_recorder(tmp_path)
    with recorder:
        with pytest.raises(RecorderSetupError, match="already open"):
            recorder.__enter__()

        def broken_factory(**_kwargs: object) -> object:
            raise RuntimeError("sensitive setup detail")

        assert recorder._sdk is not None
        monkeypatch.setattr(recorder._sdk, "DefaultHttpxClient", broken_factory)
        with pytest.raises(RecorderSetupError, match="could not create"):
            recorder.http_client()
    recorder._close()


def test_symlink_destination_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "requests.jsonl"
    target = tmp_path / "target"
    target.write_text("unused", encoding="utf-8")
    try:
        destination.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RecorderSetupError, match="unsafe"):
        new_recorder(tmp_path).__enter__()
    destination.unlink()


def test_nonregular_and_permissive_open_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFDIR))
    with pytest.raises(RecorderSetupError, match="unsafe"):
        new_recorder(tmp_path).__enter__()
    assert not (tmp_path / "requests.jsonl.lock").exists()
    monkeypatch.setattr(recording, "_WINDOWS", False)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o644),
    )
    with pytest.raises(RecorderSetupError, match="unsafe"):
        new_recorder(tmp_path).__enter__()
    assert not (tmp_path / "requests.jsonl.lock").exists()


def test_log_open_failure_releases_sidecar_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "requests.jsonl"
    real_open = os.open

    def broken_open(path: object, flags: int, mode: int = 0o777) -> int:
        if path == destination:
            raise OSError("sensitive path")
        return real_open(path, flags, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", broken_open)
    with pytest.raises(RecorderSetupError, match="unsafe"):
        new_recorder(tmp_path).__enter__()
    assert not destination.exists()
    assert not (tmp_path / "requests.jsonl.lock").exists()


@pytest.mark.parametrize("failure", ["close", "unlink"])
def test_lock_cleanup_errors_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    destination = tmp_path / "requests.jsonl"
    real_open = os.open
    real_close = os.close
    real_unlink = os.unlink

    def broken_open(path: object, flags: int, mode: int = 0o777) -> int:
        if path == destination:
            raise OSError("sensitive path")
        return real_open(path, flags, mode)  # type: ignore[arg-type]

    def broken_close(fd: int) -> None:
        real_close(fd)
        raise OSError("sensitive path")

    def broken_unlink(path: object, *args: object, **kwargs: object) -> None:
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("sensitive path")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", broken_open)
        if failure == "close":
            patch.setattr(os, "close", broken_close)
        else:
            patch.setattr(os, "unlink", broken_unlink)
        with pytest.raises(RecorderSetupError, match="unsafe"):
            new_recorder(tmp_path).__enter__()
    assert not (tmp_path / "requests.jsonl.lock").exists()


def test_close_reports_lock_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = new_recorder(tmp_path)
    recorder.__enter__()
    real_unlink = os.unlink

    def broken_unlink(path: object, *args: object, **kwargs: object) -> None:
        if str(path).endswith("requests.jsonl.lock"):
            raise OSError("sensitive path")
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", broken_unlink)
    with pytest.raises(RecorderSetupError, match="could not close"):
        recorder.__exit__(None, None, None)
    monkeypatch.setattr(os, "unlink", real_unlink)
    (tmp_path / "requests.jsonl.lock").unlink()


def test_unbuffered_metadata_is_skipped(tmp_path: Path) -> None:
    recorder = new_recorder(tmp_path)
    with pytest.warns(RecorderWarning, match="unsupported_destination"):
        with recorder:
            recorder.http_client().hook(SimpleNamespace(method="POST"))
    assert recorder.status.skipped == 1


def test_capture_failure_is_content_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = new_recorder(tmp_path)

    def broken_prepare(_request: object) -> tuple[None, None]:
        raise RuntimeError("sensitive request detail")

    monkeypatch.setattr(recorder, "_prepare", broken_prepare)
    with pytest.warns(RecorderWarning, match="write_failed"):
        with recorder:
            recorder.http_client().hook(make_request())
    assert recorder.status.failed == 1
    assert (tmp_path / "requests.jsonl").read_bytes() == b""


def test_disk_error_before_write_and_failed_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = new_recorder(tmp_path)
    real_lseek = os.lseek
    real_write = os.write

    def no_space(_fd: int, _offset: int, _how: int) -> int:
        raise OSError("sensitive path")

    with pytest.warns(RecorderWarning, match="write_failed"):
        with recorder:
            monkeypatch.setattr(os, "lseek", no_space)
            recorder.http_client().hook(make_request())
            monkeypatch.setattr(os, "lseek", real_lseek)
            recorder.http_client().hook(make_request())
    assert recorder.status.failed == 2
    assert (tmp_path / "requests.jsonl").read_bytes() == b""

    second = new_recorder(tmp_path)

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:3])

    def cannot_truncate(_fd: int, _length: int) -> None:
        raise OSError("sensitive path")

    with pytest.warns(RecorderWarning, match="write_failed"):
        with second:
            monkeypatch.setattr(os, "write", short_write)
            monkeypatch.setattr(os, "ftruncate", cannot_truncate)
            second.http_client().hook(make_request())
    assert second.status.failed == 1
    assert (tmp_path / "requests.jsonl").read_bytes() == b'{"m'


def test_async_close_waits_for_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    recorder = new_recorder(tmp_path, asynchronous=True)
    assert isinstance(recorder, AsyncRequestRecorder)
    capture = recorder._capture

    def slow_capture(request: object) -> None:
        started.set()
        release.wait(timeout=5)
        capture(request)

    monkeypatch.setattr(recorder, "_capture", slow_capture)

    async def scenario() -> None:
        await recorder.__aenter__()
        client = recorder.http_client()
        hook_task = asyncio.create_task(client.hook(make_request()))
        await asyncio.to_thread(started.wait, 5)
        closing = asyncio.create_task(recorder.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        await hook_task
        await client.hook(make_request())

    asyncio.run(scenario())
    assert recorder.status.written == 1


def test_async_executor_failure_does_not_escape_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = new_recorder(tmp_path, asynchronous=True)
    assert isinstance(recorder, AsyncRequestRecorder)

    def broken_executor(*_args: object) -> object:
        raise RuntimeError("sensitive executor detail")

    async def scenario() -> None:
        async with recorder:
            client = recorder.http_client()
            with monkeypatch.context() as patch:
                patch.setattr(
                    asyncio.get_running_loop(), "run_in_executor", broken_executor
                )
                with pytest.warns(RecorderWarning, match="write_failed"):
                    await client.hook(make_request())

    asyncio.run(scenario())
    assert recorder.status.failed == 1
