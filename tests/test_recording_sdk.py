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
