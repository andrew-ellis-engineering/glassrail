"""Tests for the OpenAI-compatible streaming provider.

A ``httpx.MockTransport`` stands in for the HTTP endpoint, returning a
buffered Server-Sent Events body that ``aiter_lines`` walks just as it
would a real streamed response.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from glassrail.providers import Message, ProviderUnavailableError, cacheable_message, collect
from glassrail.providers.openai_compat import OpenAICompatProvider

_MSG: list[Message] = [{"role": "user", "content": "hi"}]


def _sse(*events: dict[str, object]) -> str:
    """Render objects as an OpenAI-style SSE body terminated by [DONE]."""
    lines = [f"data: {json.dumps(e)}\n\n" for e in events]
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _content_event(text: str, *, finish: str | None = None) -> dict[str, object]:
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


def _provider(
    handler: httpx.MockTransport,
    *,
    api_key: str = "",
    extra_body: dict[str, object] | None = None,
    prompt_caching: bool | None = None,
    base_url: str = "http://test.local/v1",
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="tier0",
        tier=0,
        base_url=base_url,
        model="test-model",
        api_key=api_key,
        prompt_caching=prompt_caching,
        extra_body=extra_body,
        transport=handler,
    )


async def test_streams_content_in_multiple_chunks() -> None:
    body = _sse(
        _content_event("Hello"),
        _content_event(", "),
        _content_event("world", finish="stop"),
        {"choices": [], "usage": {"total_tokens": 7}},
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=body))
    provider = _provider(transport)

    chunks = [c async for c in provider.complete(_MSG)]
    text = "".join(c.text for c in chunks)
    assert text == "Hello, world"
    # More than one chunk carried text — proves real streaming, not a
    # single buffered yield.
    assert sum(1 for c in chunks if c.text) >= 3


async def test_reports_usage_and_finish_reason() -> None:
    body = _sse(
        _content_event("answer", finish="stop"),
        {"choices": [], "usage": {"total_tokens": 42}},
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=body))
    provider = _provider(transport)

    chunks = [c async for c in provider.complete(_MSG)]
    assert "".join(c.text for c in chunks) == "answer"
    # The terminal chunk carries the finish_reason and the usage total.
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].tokens_used == 42


async def test_reports_prompt_cache_usage() -> None:
    body = _sse(
        _content_event("answer", finish="stop"),
        {
            "choices": [],
            "usage": {
                "total_tokens": 42,
                "prompt_tokens_details": {
                    "cached_tokens": 30,
                    "cache_write_tokens": 10,
                },
            },
        },
    )
    provider = _provider(httpx.MockTransport(lambda _req: httpx.Response(200, text=body)))

    chunks = [chunk async for chunk in provider.complete(_MSG)]

    assert chunks[-1].cache_read_tokens == 30
    assert chunks[-1].cache_write_tokens == 10


async def test_request_sets_stream_and_auth() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, text=_sse(_content_event("ok", finish="stop")))

    provider = _provider(httpx.MockTransport(handler), api_key="secret")
    _ = [c async for c in provider.complete(_MSG, json_mode=True)]

    sent = captured["body"]
    assert isinstance(sent, dict)
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["response_format"] == {"type": "json_object"}
    assert captured["auth"] == "Bearer secret"


async def test_explicit_cache_prefixes_become_openrouter_content_blocks() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=_sse(_content_event("ok", finish="stop")))

    provider = _provider(httpx.MockTransport(handler), prompt_caching=True)
    messages = [
        cacheable_message("system", "stable system"),
        cacheable_message("user", "stable\n\ndynamic", prefix_chars=len("stable\n\n")),
    ]

    await collect(provider.complete(messages))

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["messages"] == [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "stable system",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "stable\n\n",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "dynamic"},
            ],
        },
    ]


async def test_disabled_cache_hints_leave_plain_message_content() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=_sse(_content_event("ok", finish="stop")))

    provider = _provider(httpx.MockTransport(handler), prompt_caching=False)

    await collect(provider.complete([cacheable_message("system", "unchanged")]))

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["messages"] == [{"role": "system", "content": "unchanged"}]


def test_prompt_caching_auto_enables_only_for_openrouter() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, text=_sse(_content_event("ok")))
    )
    local = _provider(transport)
    openrouter = _provider(transport, base_url="https://openrouter.ai/api/v1")

    assert local.prompt_caching is False
    assert openrouter.prompt_caching is True


async def test_reasoning_mandatory_error_retries_without_disabled_reasoning() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Reasoning is mandatory for this endpoint and cannot be disabled."
                        )
                    }
                },
            )
        return httpx.Response(200, text=_sse(_content_event("ok", finish="stop")))

    provider = _provider(
        httpx.MockTransport(handler),
        extra_body={"reasoning": {"effort": "none"}, "provider": {"require_parameters": True}},
    )

    text, _ = await collect(provider.complete(_MSG))

    assert text == "ok"
    assert bodies[0]["reasoning"] == {"effort": "none"}
    assert "reasoning" not in bodies[1]
    assert bodies[1]["provider"] == {"require_parameters": True}


async def test_tool_call_is_synthesised_into_content() -> None:
    """A streamed tool call with no content becomes JSON content."""
    body = _sse(
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"name": "make_plan"}}]},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"x"'}}]}}]},
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=body))
    provider = _provider(transport)

    text, _ = await collect(provider.complete(_MSG))
    parsed = json.loads(text)
    assert parsed == {"tool_call": "make_plan", "arguments": {"x": 1}}


async def test_connect_error_becomes_provider_unavailable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailableError, match="ConnectError"):
        _ = [c async for c in provider.complete(_MSG)]


async def test_timeout_becomes_provider_unavailable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    provider = _provider(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailableError, match="ReadTimeout"):
        _ = [c async for c in provider.complete(_MSG)]


async def test_http_5xx_falls_through() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="boom"))
    provider = _provider(transport)
    with pytest.raises(ProviderUnavailableError, match="500"):
        _ = [c async for c in provider.complete(_MSG)]


async def test_malformed_sse_lines_are_skipped() -> None:
    body = (
        "data: not-json-at-all\n\n"
        ": this is an SSE comment\n\n"
        f"data: {json.dumps(_content_event('clean', finish='stop'))}\n\n"
        "data: [DONE]\n\n"
    )
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=body))
    provider = _provider(transport)

    text, _ = await collect(provider.complete(_MSG))
    assert text == "clean"


async def test_reuses_async_client_between_complete_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    clients: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=_sse(_content_event(f"ok-{calls}", finish="stop")))

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = real_client(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    provider = _provider(httpx.MockTransport(handler))
    try:
        first, _ = await collect(provider.complete(_MSG))
        second, _ = await collect(provider.complete(_MSG))

        assert first == "ok-1"
        assert second == "ok-2"
        assert len(clients) == 1
    finally:
        await provider.aclose()

    assert clients[0].is_closed
