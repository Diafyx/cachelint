"""Opt-in, local recording of supported SDK request bodies.

Recorded prompts remain sensitive. This module never records transport metadata.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import threading
import warnings
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from cachelint._ingest import Provider, canonicalize_request
from cachelint._usage import UsageBuffer, UsageStatus, parse_usage

_VERSIONS = {"anthropic": "1.8.0", "openai": "3.37.0"}
_ENDPOINTS = {
    "anthropic": ("api.anthropic.com", "/v1/messages"),
    "openai": ("api.openai.com", "/v1/chat/completions"),
}
_CREDENTIAL_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "xapikey",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "password",
        "secret",
        "privatekey",
        "token",
    }
)
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_PENDING_USAGE = 4096
_WINDOWS = os.name == "nt"


class RecorderSetupError(ValueError):
    """The recorder could not be opened safely; no request was captured."""


class UnsupportedSDKError(RecorderSetupError):
    """The selected provider SDK is missing or is not the reviewed version."""


class RecorderWarning(UserWarning):
    """A content-free recording failure code."""


@dataclass(frozen=True)
class RecorderStatus:
    """Content-free snapshot of recording attempts."""

    written: int
    skipped: int
    failed: int
    last_failure_code: str | None
    usage_written: int = 0
    usage_failed: int = 0


@dataclass(frozen=True)
class _PendingUsage:
    request: object
    attempt_id: int
    capture_status: str
    capture_code: str | None
    request_offset: int | None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")


def _contains_credential(value: object) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key.casefold().replace("_", "").replace("-", "") in _CREDENTIAL_KEYS:
                    return True
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)
    return False


class _RecorderBase:
    def __init__(
        self,
        path: str | Path,
        *,
        provider: Provider,
        allow_windows_acl_inheritance: bool = False,
        capture_usage: bool = False,
    ) -> None:
        if provider not in _VERSIONS:
            raise RecorderSetupError("unsupported provider")
        try:
            selected = Path(path)
        except Exception:
            raise RecorderSetupError("invalid recording path") from None
        if not selected.is_absolute():
            raise RecorderSetupError("recording path must be absolute")
        self._path = selected
        self._provider = provider
        self._allow_windows_acl_inheritance = allow_windows_acl_inheritance
        self._capture_usage = capture_usage
        self._mutex = threading.RLock()
        self._fd: int | None = None
        self._usage_fd: int | None = None
        self._lock_fd: int | None = None
        self._lock_path = Path(str(selected) + ".lock")
        self._usage_path = Path(str(selected) + ".usage.jsonl")
        self._sdk: ModuleType | None = None
        self._broken = False
        self._usage_broken = False
        self._attempts: dict[int, _PendingUsage] = {}
        self._next_attempt_id = 1
        self._warned: set[str] = set()
        self._written = 0
        self._skipped = 0
        self._failed = 0
        self._last_failure_code: str | None = None
        self._usage_written = 0
        self._usage_failed = 0

    @property
    def status(self) -> RecorderStatus:
        with self._mutex:
            return RecorderStatus(
                self._written,
                self._skipped,
                self._failed,
                self._last_failure_code,
                self._usage_written,
                self._usage_failed,
            )

    def _load_sdk(self) -> ModuleType:
        try:
            installed = metadata.version(self._provider)
        except Exception:
            raise UnsupportedSDKError(
                "supported provider SDK is not installed"
            ) from None
        if installed != _VERSIONS[self._provider]:
            raise UnsupportedSDKError("provider SDK version is not supported")
        try:
            return importlib.import_module(self._provider)
        except Exception:
            raise UnsupportedSDKError(
                "supported provider SDK is not installed"
            ) from None

    @staticmethod
    def _check_destination(path: Path) -> None:
        try:
            target = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(target.st_mode):
            raise RecorderSetupError("recording destination must be a regular file")

    @staticmethod
    def _open_destination(path: Path, *, exclusive: bool = False) -> int:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        if exclusive:
            flags |= os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise RecorderSetupError("recording destination must be a regular file")
            if not _WINDOWS and opened.st_mode & 0o077:
                raise RecorderSetupError("recording file must be owner-only")
        except Exception:
            os.close(fd)
            raise
        return fd

    def _open(self) -> None:
        if self._fd is not None:
            raise RecorderSetupError("recorder is already open")
        if _WINDOWS and not self._allow_windows_acl_inheritance:
            raise RecorderSetupError("Windows ACL inheritance requires acknowledgement")
        sdk = self._load_sdk()
        try:
            if not self._path.parent.is_dir():
                raise RecorderSetupError("recording parent directory must exist")
            self._check_destination(self._path)
            if self._capture_usage:
                try:
                    self._usage_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise RecorderSetupError("usage sidecar must be new")
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            self._lock_fd = os.open(
                self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600
            )
            try:
                self._fd = self._open_destination(self._path)
                if self._capture_usage:
                    self._usage_fd = self._open_destination(
                        self._usage_path, exclusive=True
                    )
            except Exception:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                raise
        except (OSError, RecorderSetupError):
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
                self._lock_fd = None
                try:
                    os.unlink(self._lock_path)
                except OSError:
                    pass
            raise RecorderSetupError("unsafe or unavailable recording path") from None
        self._sdk = sdk

    def _close(self) -> None:
        with self._mutex:
            if self._fd is None:
                return
            fd = self._fd
            usage_fd = self._usage_fd
            lock_fd = self._lock_fd
            for pending in tuple(self._attempts.values()):
                self._write_usage(pending, "unmatched", {})
            self._attempts.clear()
            self._fd = None
            self._usage_fd = None
            self._lock_fd = None
            self._sdk = None
            try:
                os.close(fd)
                if usage_fd is not None:
                    os.close(usage_fd)
                os.close(cast(int, lock_fd))
                os.unlink(self._lock_path)
            except OSError:
                raise RecorderSetupError("could not close recorder safely") from None

    def _report(self, code: str, *, failed: bool = False) -> None:
        with self._mutex:
            if failed:
                self._failed += 1
            else:
                self._skipped += 1
            self._last_failure_code = code
            first = code not in self._warned
            self._warned.add(code)
        if first:
            try:
                warnings.warn(code, RecorderWarning, stacklevel=3)
            except Exception:
                # A warnings-as-errors filter must not alter the SDK call.
                pass

    def _report_usage_failure(self) -> None:
        with self._mutex:
            self._usage_failed += 1
            self._last_failure_code = "usage_write_failed"
            first = "usage_write_failed" not in self._warned
            self._warned.add("usage_write_failed")
        if first:
            try:
                warnings.warn("usage_write_failed", RecorderWarning, stacklevel=3)
            except Exception:
                pass

    def _write_usage(
        self, pending: _PendingUsage, status: UsageStatus, counters: dict[str, int]
    ) -> None:
        with self._mutex:
            if self._usage_fd is None or self._usage_broken:
                self._report_usage_failure()
                return
            row: dict[str, object] = {
                "schema_version": 1,
                "attempt_id": pending.attempt_id,
                "capture_status": pending.capture_status,
                "usage_status": status,
            }
            if pending.capture_code is not None:
                row["capture_code"] = pending.capture_code
            if pending.request_offset is not None:
                row["request_offset"] = pending.request_offset
            if counters:
                row["usage"] = counters
            encoded = (json.dumps(row, separators=(",", ":")) + "\n").encode("ascii")
            previous: int | None = None
            try:
                previous = os.lseek(self._usage_fd, 0, os.SEEK_END)
                if os.write(self._usage_fd, encoded) != len(encoded):
                    raise OSError("short write")
                os.fsync(self._usage_fd)
                self._usage_written += 1
            except OSError:
                self._usage_broken = True
                if previous is not None:
                    try:
                        os.ftruncate(self._usage_fd, previous)
                    except OSError:
                        pass
                self._report_usage_failure()

    def _eligible(self, request: Any) -> bool:
        try:
            host, path = _ENDPOINTS[self._provider]
            url = request.url
            return not (
                request.method != "POST"
                or url.scheme != "https"
                or url.host != host
                or url.path != path
                or url.port not in (None, 443)
                or url.username
                or url.password
                or url.query
            )
        except Exception:
            return False

    def _prepare(self, request: Any) -> tuple[bytes | None, str | None]:
        if not self._eligible(request):
            return None, "unsupported_destination"
        try:
            raw = request.content
        except Exception:
            return None, "nonbuffered_body"
        try:
            body = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant,
            )
            if not isinstance(body, dict):
                return None, "invalid_body"
            if _contains_credential(body):
                return None, "credential_field"
            canonicalize_request(body, provider=self._provider)
            encoded = (
                json.dumps(
                    body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
            )
        except Exception:
            return None, "invalid_body"
        if len(encoded) > _MAX_RECORD_BYTES:
            return None, "size_limit"
        return encoded, None

    def _append(self, encoded: bytes) -> int | None:
        with self._mutex:
            if self._fd is None or self._broken:
                return None
            previous: int | None = None
            try:
                previous = os.lseek(self._fd, 0, os.SEEK_END)
                if os.write(self._fd, encoded) != len(encoded):
                    raise OSError("short write")
                os.fsync(self._fd)
                self._written += 1
                return previous
            except OSError:
                self._broken = True
                if previous is not None:
                    try:
                        os.ftruncate(self._fd, previous)
                    except OSError:
                        pass
                return None

    def _capture(self, request: Any) -> tuple[str, str | None, int | None]:
        try:
            encoded, code = self._prepare(request)
            if code is not None:
                self._report(code)
                return "skipped", code, None
            if encoded is None:
                self._report("invalid_body")
                return "skipped", "invalid_body", None
            offset = self._append(encoded)
            if offset is None:
                self._report("write_failed", failed=True)
                return "failed", "write_failed", None
            return "written", None, offset
        except Exception:
            # SDK traffic takes precedence over all ordinary capture failures.
            self._report("write_failed", failed=True)
            return "failed", "write_failed", None

    def _remember(
        self, request: Any, result: tuple[str, str | None, int | None]
    ) -> None:
        with self._mutex:
            if self._usage_fd is None or result[1] == "unsupported_destination":
                return
            key = id(request)
            prior = self._attempts.pop(key, None)
            if prior is not None:
                self._write_usage(prior, "unmatched", {})
            if len(self._attempts) >= _MAX_PENDING_USAGE:
                oldest = next(iter(self._attempts))
                self._write_usage(self._attempts.pop(oldest), "unmatched", {})
            pending = _PendingUsage(request, self._next_attempt_id, *result)
            self._next_attempt_id += 1
            self._attempts[key] = pending

    def _finish_usage(
        self,
        request: Any,
        attempt_id: int,
        status: UsageStatus,
        counters: dict[str, int],
    ) -> None:
        with self._mutex:
            key = id(request)
            pending = self._attempts.get(key)
            if (
                pending is None
                or pending.request is not request
                or pending.attempt_id != attempt_id
            ):
                return
            del self._attempts[key]
            self._write_usage(pending, status, counters)

    def _on_request(self, request: Any) -> None:
        self._remember(request, self._capture(request))

    def _response_pending(self, response: Any) -> tuple[Any, _PendingUsage] | None:
        try:
            request = response.request
        except Exception:
            return None
        if not self._eligible(request):
            return None
        with self._mutex:
            if self._usage_fd is None:
                return None
            key = id(request)
            pending = self._attempts.get(key)
            if pending is None or pending.request is not request:
                pending = _PendingUsage(
                    request, self._next_attempt_id, "unmatched", None, None
                )
                self._next_attempt_id += 1
                self._attempts[key] = pending
            return request, pending

    def _response_start(
        self, response: Any, request: Any, pending: _PendingUsage
    ) -> bool:
        try:
            if not 200 <= response.status_code < 300:
                self._finish_usage(request, pending.attempt_id, "http_error", {})
                return False
            if "text/event-stream" in response.headers.get("content-type", "").lower():
                self._finish_usage(request, pending.attempt_id, "streaming", {})
                return False
            try:
                buffered = response.content
            except Exception:
                buffered = None
            if isinstance(buffered, bytes):
                status, counters = parse_usage(self._provider, buffered)
                self._finish_usage(request, pending.attempt_id, status, counters)
                return False
            return True
        except Exception:
            self._finish_usage(request, pending.attempt_id, "incomplete", {})
            return False

    def _on_response(self, response: Any) -> None:
        match = self._response_pending(response)
        if match is None:
            return
        request, pending = match
        if not self._response_start(response, request, pending):
            return
        try:
            original = response.iter_bytes

            def observed(chunk_size: int | None = None) -> Any:
                buffer = UsageBuffer()
                complete = False
                try:
                    for chunk in original(chunk_size=chunk_size):
                        buffer.add(chunk)
                        yield chunk
                    complete = True
                finally:
                    status, counters = (
                        buffer.result(self._provider)
                        if complete
                        else ("incomplete", {})
                    )
                    self._finish_usage(request, pending.attempt_id, status, counters)

            response.iter_bytes = observed
        except Exception:
            self._finish_usage(request, pending.attempt_id, "incomplete", {})

    async def _async_on_response(self, response: Any) -> None:
        match = self._response_pending(response)
        if match is None:
            return
        request, pending = match
        if not self._response_start(response, request, pending):
            return
        try:
            original = response.aiter_bytes

            async def observed(chunk_size: int | None = None) -> Any:
                buffer = UsageBuffer()
                complete = False
                try:
                    async for chunk in original(chunk_size=chunk_size):
                        buffer.add(chunk)
                        yield chunk
                    complete = True
                finally:
                    status, counters = (
                        buffer.result(self._provider)
                        if complete
                        else ("incomplete", {})
                    )
                    await asyncio.to_thread(
                        self._finish_usage,
                        request,
                        pending.attempt_id,
                        status,
                        counters,
                    )

            response.aiter_bytes = observed
        except Exception:
            await asyncio.to_thread(
                self._finish_usage, request, pending.attempt_id, "incomplete", {}
            )

    def _http_client(self, name: str, hook: Any, response_hook: Any = None) -> Any:
        if self._fd is None or self._sdk is None:
            raise RecorderSetupError("recorder is not open")
        try:
            factory = getattr(self._sdk, name)
            hooks = {"request": [hook]}
            if response_hook is not None:
                hooks["response"] = [response_hook]
            return factory(event_hooks=hooks)
        except Exception:
            raise RecorderSetupError("could not create SDK HTTP client") from None


class RequestRecorder(_RecorderBase):
    """Synchronous recorder; close the SDK client before leaving its context."""

    def __enter__(self) -> RequestRecorder:
        self._open()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._close()

    def http_client(self) -> Any:
        name = (
            "DefaultHttpxClient"
            if self._provider == "anthropic"
            else "DefaultHttpx2Client"
        )
        if self._capture_usage:
            return self._http_client(name, self._on_request, self._on_response)
        return self._http_client(name, self._capture)


class AsyncRequestRecorder(_RecorderBase):
    """Asynchronous recorder; close the SDK client before leaving its context."""

    def __init__(
        self,
        path: str | Path,
        *,
        provider: Provider,
        allow_windows_acl_inheritance: bool = False,
        capture_usage: bool = False,
    ) -> None:
        super().__init__(
            path,
            provider=provider,
            allow_windows_acl_inheritance=allow_windows_acl_inheritance,
            capture_usage=capture_usage,
        )
        self._pending: set[asyncio.Future[Any]] = set()
        self._closing = False

    async def __aenter__(self) -> AsyncRequestRecorder:
        self._open()
        self._closing = False
        return self

    async def __aexit__(
        self, _type: object, _value: object, _traceback: object
    ) -> None:
        self._closing = True
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        self._close()

    async def _async_capture(self, request: Any) -> None:
        if self._closing:
            return
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, self._capture, request)
            self._pending.add(future)
            future.add_done_callback(self._pending.discard)
            result = await asyncio.shield(future)
            if self._capture_usage:
                self._remember(request, result)
        except Exception:
            self._report("write_failed", failed=True)
            if self._capture_usage:
                self._remember(request, ("failed", "write_failed", None))

    def http_client(self) -> Any:
        name = (
            "DefaultAsyncHttpxClient"
            if self._provider == "anthropic"
            else "DefaultAsyncHttpx2Client"
        )
        if self._capture_usage:
            return self._http_client(name, self._async_capture, self._async_on_response)
        return self._http_client(name, self._async_capture)


__all__ = [
    "AsyncRequestRecorder",
    "RecorderSetupError",
    "RecorderStatus",
    "RecorderWarning",
    "RequestRecorder",
    "UnsupportedSDKError",
]
