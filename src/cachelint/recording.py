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
        self._mutex = threading.RLock()
        self._fd: int | None = None
        self._lock_fd: int | None = None
        self._lock_path = Path(str(selected) + ".lock")
        self._sdk: ModuleType | None = None
        self._broken = False
        self._warned: set[str] = set()
        self._written = 0
        self._skipped = 0
        self._failed = 0
        self._last_failure_code: str | None = None

    @property
    def status(self) -> RecorderStatus:
        with self._mutex:
            return RecorderStatus(
                self._written, self._skipped, self._failed, self._last_failure_code
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

    def _open(self) -> None:
        if self._fd is not None:
            raise RecorderSetupError("recorder is already open")
        if _WINDOWS and not self._allow_windows_acl_inheritance:
            raise RecorderSetupError("Windows ACL inheritance requires acknowledgement")
        sdk = self._load_sdk()
        try:
            if not self._path.parent.is_dir():
                raise RecorderSetupError("recording parent directory must exist")
            try:
                target = self._path.lstat()
            except FileNotFoundError:
                target = None
            if target is not None and not stat.S_ISREG(target.st_mode):
                raise RecorderSetupError("recording destination must be a regular file")
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            self._lock_fd = os.open(
                self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600
            )
            try:
                self._fd = os.open(self._path, flags | nofollow, 0o600)
                opened = os.fstat(self._fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise RecorderSetupError(
                        "recording destination must be a regular file"
                    )
                if not _WINDOWS and opened.st_mode & 0o077:
                    raise RecorderSetupError("recording file must be owner-only")
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
            lock_fd = self._lock_fd
            self._fd = None
            self._lock_fd = None
            self._sdk = None
            try:
                os.close(fd)
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

    def _prepare(self, request: Any) -> tuple[bytes | None, str | None]:
        try:
            host, path = _ENDPOINTS[self._provider]
            url = request.url
            if (
                request.method != "POST"
                or url.scheme != "https"
                or url.host != host
                or url.path != path
                or url.port not in (None, 443)
                or url.username
                or url.password
                or url.query
            ):
                return None, "unsupported_destination"
        except Exception:
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

    def _append(self, encoded: bytes) -> bool:
        with self._mutex:
            if self._fd is None or self._broken:
                return False
            previous: int | None = None
            try:
                previous = os.lseek(self._fd, 0, os.SEEK_END)
                if os.write(self._fd, encoded) != len(encoded):
                    raise OSError("short write")
                os.fsync(self._fd)
                self._written += 1
                return True
            except OSError:
                self._broken = True
                if previous is not None:
                    try:
                        os.ftruncate(self._fd, previous)
                    except OSError:
                        pass
                return False

    def _capture(self, request: Any) -> None:
        try:
            encoded, code = self._prepare(request)
            if code is not None:
                self._report(code)
            elif encoded is not None and not self._append(encoded):
                self._report("write_failed", failed=True)
        except Exception:
            # SDK traffic takes precedence over all ordinary capture failures.
            self._report("write_failed", failed=True)

    def _http_client(self, name: str, hook: Any) -> Any:
        if self._fd is None or self._sdk is None:
            raise RecorderSetupError("recorder is not open")
        try:
            factory = getattr(self._sdk, name)
            return factory(event_hooks={"request": [hook]})
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
        return self._http_client(name, self._capture)


class AsyncRequestRecorder(_RecorderBase):
    """Asynchronous recorder; close the SDK client before leaving its context."""

    def __init__(
        self,
        path: str | Path,
        *,
        provider: Provider,
        allow_windows_acl_inheritance: bool = False,
    ) -> None:
        super().__init__(
            path,
            provider=provider,
            allow_windows_acl_inheritance=allow_windows_acl_inheritance,
        )
        self._pending: set[asyncio.Future[None]] = set()
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
            await asyncio.shield(future)
        except Exception:
            self._report("write_failed", failed=True)

    def http_client(self) -> Any:
        name = (
            "DefaultAsyncHttpxClient"
            if self._provider == "anthropic"
            else "DefaultAsyncHttpx2Client"
        )
        return self._http_client(name, self._async_capture)


__all__ = [
    "AsyncRequestRecorder",
    "RecorderSetupError",
    "RecorderStatus",
    "RecorderWarning",
    "RequestRecorder",
    "UnsupportedSDKError",
]
