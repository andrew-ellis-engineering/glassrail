"""Tests for the OpenAI-compatible streaming provider.

A ``httpx.MockTransport`` stands in for the HTTP endpoint, returning a
buffered Server-Sent Events body that ``aiter_lines`` walks just as it
would a real streamed response.
"""

from __future__ import annotations

import json

import httpx
import pytest

from dagagent.providers import Message, ProviderUnavailableError, collect
from dagagent.providers.openai_compat import OpenAICompatProvider

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
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="tier0",
        tier=0,
        base_url="http://test.local/v1",
        model="test-model",
        api_key=api_key,
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


async def test_http_error_status_raises() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(500, text="boom"))
    provider = _provider(transport)
    with pytest.raises(httpx.HTTPStatusError):
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
