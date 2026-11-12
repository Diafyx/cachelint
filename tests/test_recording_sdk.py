"""Pinned SDK integrations use mock transports and synthetic request bodies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from cachelint import Provider
from cachelint._ingest import read_jsonl
from cachelint.recording import AsyncRequestRecorder, RequestRecorder

_ANTHROPIC_REPLY: dict[str, object] = {
    "id": "msg_synthetic",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-6",
    "content": [],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
_OPENAI_REPLY: dict[str, object] = {
    "id": "chatcmpl_synthetic",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o",
    "choices": [],
}


def _sdk(provider: Provider) -> tuple[Any, Any]:
    sdk = cast(Any, pytest.importorskip(provider))
    httpx2 = cast(Any, pytest.importorskip("httpx2"))
    return sdk, httpx2


def _call(client: Any, provider: Provider) -> Any:
    if provider == "anthropic":
        return client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16,
            system="Synthetic instructions",
            messages=[{"role": "user", "content": "Synthetic prompt"}],
        )
    return client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Synthetic prompt"}],
    )


def _reply(provider: Provider) -> dict[str, object]:
    return _ANTHROPIC_REPLY if provider == "anthropic" else _OPENAI_REPLY


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_pinned_sync_sdk_preserves_sent_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    sdk, httpx2 = _sdk(provider)
    factory_name = (
        "DefaultHttpxClient" if provider == "anthropic" else "DefaultHttpx2Client"
    )
    original_factory = getattr(sdk, factory_name)
    sent: list[bytes] = []

    def handler(request: Any) -> Any:
        sent.append(request.content)
        return httpx2.Response(200, request=request, json=_reply(provider))

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, factory_name, factory)
    path = tmp_path / "captured.jsonl"
    with RequestRecorder(
        path,
        provider=provider,
        allow_windows_acl_inheritance=True,
    ) as recorder:
        with (
            sdk.Anthropic(
                api_key="sk-synthetic",
                http_client=recorder.http_client(),
                max_retries=0,
            )
            if provider == "anthropic"
            else sdk.OpenAI(
                api_key="sk-synthetic",
                http_client=recorder.http_client(),
                max_retries=0,
            ) as client
        ):
            _call(client, provider)
    assert len(sent) == recorder.status.written == 1
    assert json.loads(path.read_bytes()) == json.loads(sent[0])
    assert len(read_jsonl(path, provider=provider)) == 1
    assert b"sk-synthetic" not in path.read_bytes()


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_pinned_async_sdk_preserves_sent_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    sdk, httpx2 = _sdk(provider)
    factory_name = (
        "DefaultAsyncHttpxClient"
        if provider == "anthropic"
        else "DefaultAsyncHttpx2Client"
    )
    original_factory = getattr(sdk, factory_name)
    sent: list[bytes] = []

    async def handler(request: Any) -> Any:
        sent.append(request.content)
        return httpx2.Response(200, request=request, json=_reply(provider))

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, factory_name, factory)
    path = tmp_path / "captured.jsonl"

    async def scenario() -> int:
        async with AsyncRequestRecorder(
            path,
            provider=provider,
            allow_windows_acl_inheritance=True,
        ) as recorder:
            client_type = (
                sdk.AsyncAnthropic if provider == "anthropic" else sdk.AsyncOpenAI
            )
            async with client_type(
                api_key="sk-synthetic",
                http_client=recorder.http_client(),
                max_retries=0,
            ) as client:
                await _call(client, provider)
        return recorder.status.written

    assert asyncio.run(scenario()) == 1
    assert len(sent) == 1
    assert json.loads(path.read_bytes()) == json.loads(sent[0])
    assert len(read_jsonl(path, provider=provider)) == 1


def test_plain_sdk_client_creates_no_recording(tmp_path: Path) -> None:
    sdk, httpx2 = _sdk("openai")
    sent: list[bytes] = []

    def handler(request: Any) -> Any:
        sent.append(request.content)
        return httpx2.Response(200, request=request, json=_OPENAI_REPLY)

    with sdk.OpenAI(
        api_key="sk-synthetic",
        http_client=sdk.DefaultHttpx2Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    ) as client:
        _call(client, "openai")
    assert len(sent) == 1
    assert not list(tmp_path.iterdir())


def test_openai_nested_credential_is_omitted_but_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk, httpx2 = _sdk("openai")
    original_factory = sdk.DefaultHttpx2Client
    sent: list[bytes] = []

    def handler(request: Any) -> Any:
        sent.append(request.content)
        return httpx2.Response(200, request=request, json=_OPENAI_REPLY)

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, "DefaultHttpx2Client", factory)
    path = tmp_path / "captured.jsonl"
    with pytest.warns(UserWarning, match="credential_field"):
        with RequestRecorder(
            path, provider="openai", allow_windows_acl_inheritance=True
        ) as recorder:
            with sdk.OpenAI(
                api_key="sk-synthetic",
                http_client=recorder.http_client(),
                max_retries=0,
            ) as client:
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "Synthetic prompt"}],
                    extra_body={"nested": {"api_key": "synthetic"}},
                )
    assert len(sent) == 1
    assert b"api_key" in sent[0]
    assert path.read_bytes() == b""
    assert recorder.status.skipped == 1


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_pinned_sync_sdk_records_only_allowlisted_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    sdk, httpx2 = _sdk(provider)
    factory_name = (
        "DefaultHttpxClient" if provider == "anthropic" else "DefaultHttpx2Client"
    )
    original_factory = getattr(sdk, factory_name)
    reply = dict(_reply(provider))
    if provider == "anthropic":
        reply["usage"] = {
            "input_tokens": 110,
            "output_tokens": 12,
            "cache_read_input_tokens": 70,
            "cache_creation_input_tokens": 5,
            "ignored_counter": 99,
        }
        expected = {
            "input_tokens": 110,
            "output_tokens": 12,
            "cache_read_input_tokens": 70,
            "cache_creation_input_tokens": 5,
        }
    else:
        reply["usage"] = {
            "prompt_tokens": 110,
            "completion_tokens": 12,
            "total_tokens": 122,
            "prompt_tokens_details": {"cached_tokens": 70, "ignored": 99},
        }
        expected = {
            "prompt_tokens": 110,
            "completion_tokens": 12,
            "total_tokens": 122,
            "cached_tokens": 70,
        }
    reply["private_response_text"] = "must not persist"
    sent: list[bytes] = []

    def handler(request: Any) -> Any:
        sent.append(request.content)
        return httpx2.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            stream=httpx2.ByteStream(json.dumps(reply).encode()),
        )

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, factory_name, factory)
    path = tmp_path / "requests.jsonl"
    with RequestRecorder(
        path,
        provider=provider,
        capture_usage=True,
        allow_windows_acl_inheritance=True,
    ) as recorder:
        client_type = sdk.Anthropic if provider == "anthropic" else sdk.OpenAI
        with client_type(
            api_key="sk-synthetic", http_client=recorder.http_client(), max_retries=0
        ) as client:
            _call(client, provider)
    assert recorder.status.written == recorder.status.usage_written == 1
    assert recorder.status.usage_failed == 0
    assert len(sent) == len(read_jsonl(path, provider=provider)) == 1
    assert json.loads(path.read_bytes()) == json.loads(sent[0])
    sidecar = (tmp_path / "requests.jsonl.usage.jsonl").read_bytes()
    assert json.loads(sidecar) == {
        "schema_version": 1,
        "attempt_id": 1,
        "capture_status": "written",
        "request_offset": 0,
        "usage_status": "reported",
        "usage": expected,
    }
    for forbidden in (b"must not persist", b"sk-synthetic", b"private_response_text"):
        assert forbidden not in sidecar


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_pinned_async_sdk_records_usage_without_changing_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    sdk, httpx2 = _sdk(provider)
    factory_name = (
        "DefaultAsyncHttpxClient"
        if provider == "anthropic"
        else "DefaultAsyncHttpx2Client"
    )
    original_factory = getattr(sdk, factory_name)
    reply = dict(_reply(provider))
    reply["usage"] = (
        {"input_tokens": 10, "cache_read_input_tokens": 5}
        if provider == "anthropic"
        else {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 5}}
    )

    async def handler(request: Any) -> Any:
        return httpx2.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            stream=httpx2.ByteStream(json.dumps(reply).encode()),
        )

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, factory_name, factory)
    path = tmp_path / "requests.jsonl"

    async def scenario() -> tuple[Any, AsyncRequestRecorder]:
        async with AsyncRequestRecorder(
            path,
            provider=provider,
            capture_usage=True,
            allow_windows_acl_inheritance=True,
        ) as recorder:
            client_type = (
                sdk.AsyncAnthropic if provider == "anthropic" else sdk.AsyncOpenAI
            )
            async with client_type(
                api_key="sk-synthetic",
                http_client=recorder.http_client(),
                max_retries=0,
            ) as client:
                response = await _call(client, provider)
        return response, recorder

    response, recorder = asyncio.run(scenario())
    assert response.usage is not None
    assert recorder.status.written == recorder.status.usage_written == 1
    row = json.loads((tmp_path / "requests.jsonl.usage.jsonl").read_bytes())
    assert row["usage_status"] == "reported"
    cache_key = (
        "cache_read_input_tokens" if provider == "anthropic" else "cached_tokens"
    )
    assert row["usage"][cache_key] == 5


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_sdk_retry_keeps_attempt_usage_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    sdk, httpx2 = _sdk(provider)
    factory_name = (
        "DefaultHttpxClient" if provider == "anthropic" else "DefaultHttpx2Client"
    )
    original_factory = getattr(sdk, factory_name)
    attempts = 0

    def handler(request: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx2.Response(
                500,
                request=request,
                json={"error": {"message": "synthetic retry", "type": "server_error"}},
            )
        reply = dict(_reply(provider))
        reply["usage"] = (
            {"input_tokens": 10, "cache_read_input_tokens": 4}
            if provider == "anthropic"
            else {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 4}}
        )
        return httpx2.Response(200, request=request, json=reply)

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, factory_name, factory)
    path = tmp_path / "requests.jsonl"
    with RequestRecorder(
        path,
        provider=provider,
        capture_usage=True,
        allow_windows_acl_inheritance=True,
    ) as recorder:
        client_type = sdk.Anthropic if provider == "anthropic" else sdk.OpenAI
        with client_type(
            api_key="sk-synthetic", http_client=recorder.http_client(), max_retries=1
        ) as client:
            _call(client, provider)
    rows = [
        json.loads(line)
        for line in path.with_name(path.name + ".usage.jsonl").read_text().splitlines()
    ]
    assert attempts == recorder.status.written == recorder.status.usage_written == 2
    assert [row["attempt_id"] for row in rows] == [1, 2]
    assert [row["usage_status"] for row in rows] == ["http_error", "reported"]
    assert rows[0].get("usage") is None
    assert (
        rows[1]["usage"][
            "cache_read_input_tokens" if provider == "anthropic" else "cached_tokens"
        ]
        == 4
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_sdk_missing_usage_and_transport_failure_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    sdk, httpx2 = _sdk(provider)
    factory_name = (
        "DefaultHttpxClient" if provider == "anthropic" else "DefaultHttpx2Client"
    )
    original_factory = getattr(sdk, factory_name)
    attempts = 0

    def handler(request: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise httpx2.ConnectError("private transport detail", request=request)
        reply = dict(_reply(provider))
        reply.pop("usage", None)
        return httpx2.Response(200, request=request, json=reply)

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, factory_name, factory)
    path = tmp_path / "requests.jsonl"
    with RequestRecorder(
        path,
        provider=provider,
        capture_usage=True,
        allow_windows_acl_inheritance=True,
    ) as recorder:
        client_type = sdk.Anthropic if provider == "anthropic" else sdk.OpenAI
        with client_type(
            api_key="sk-synthetic", http_client=recorder.http_client(), max_retries=0
        ) as client:
            _call(client, provider)
            with pytest.raises(sdk.APIConnectionError):
                _call(client, provider)
    sidecar = path.with_name(path.name + ".usage.jsonl").read_bytes()
    rows = [json.loads(line) for line in sidecar.splitlines()]
    assert attempts == recorder.status.written == recorder.status.usage_written == 2
    assert [row["attempt_id"] for row in rows] == [1, 2]
    assert [row["usage_status"] for row in rows] == ["absent", "unmatched"]
    assert b"private transport detail" not in sidecar


def test_skipped_request_body_does_not_reassign_observed_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk, httpx2 = _sdk("openai")
    original_factory = sdk.DefaultHttpx2Client

    def handler(request: Any) -> Any:
        reply = dict(_OPENAI_REPLY)
        reply["usage"] = {
            "prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 5},
        }
        return httpx2.Response(200, request=request, json=reply)

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, "DefaultHttpx2Client", factory)
    path = tmp_path / "requests.jsonl"
    with pytest.warns(UserWarning, match="credential_field"):
        with RequestRecorder(
            path,
            provider="openai",
            capture_usage=True,
            allow_windows_acl_inheritance=True,
        ) as recorder:
            with sdk.OpenAI(
                api_key="sk-synthetic",
                http_client=recorder.http_client(),
                max_retries=0,
            ) as client:
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "Synthetic prompt"}],
                    extra_body={"nested": {"api_key": "synthetic"}},
                )
    assert path.read_bytes() == b""
    assert recorder.status.skipped == recorder.status.usage_written == 1
    row = json.loads(path.with_name(path.name + ".usage.jsonl").read_bytes())
    assert row["attempt_id"] == 1
    assert row["capture_status"] == "skipped"
    assert row["capture_code"] == "credential_field"
    assert row["usage_status"] == "reported"
    assert row["usage"]["cached_tokens"] == 5


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_sdk_streaming_response_is_left_unread_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    sdk, httpx2 = _sdk(provider)
    factory_name = (
        "DefaultHttpxClient" if provider == "anthropic" else "DefaultHttpx2Client"
    )
    original_factory = getattr(sdk, factory_name)
    response_body = b"data: private streamed content\n\n"

    def handler(request: Any) -> Any:
        return httpx2.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=httpx2.ByteStream(response_body),
        )

    def factory(**kwargs: Any) -> Any:
        return original_factory(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sdk, factory_name, factory)
    path = tmp_path / "requests.jsonl"
    with RequestRecorder(
        path,
        provider=provider,
        capture_usage=True,
        allow_windows_acl_inheritance=True,
    ) as recorder:
        client_type = sdk.Anthropic if provider == "anthropic" else sdk.OpenAI
        with client_type(
            api_key="sk-synthetic", http_client=recorder.http_client(), max_retries=0
        ) as client:
            stream = (
                client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=16,
                    messages=[{"role": "user", "content": "Synthetic prompt"}],
                    stream=True,
                )
                if provider == "anthropic"
                else client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "Synthetic prompt"}],
                    stream=True,
                )
            )
            stream.close()
    row = json.loads(path.with_name(path.name + ".usage.jsonl").read_bytes())
    assert row["capture_status"] == "written"
    assert row["usage_status"] == "streaming"
    assert (
        b"private streamed content"
        not in path.with_name(path.name + ".usage.jsonl").read_bytes()
    )
    assert recorder.status.usage_written == 1
