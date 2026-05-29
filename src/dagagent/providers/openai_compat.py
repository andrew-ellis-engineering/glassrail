"""OpenAI-compatible chat-completions provider.

Talks to any service exposing ``POST /chat/completions`` with OpenAI's
request/response shape — OpenAI, OpenRouter, Anthropic-through-OpenRouter,
local MLX through Rapid-MLX, Ollama, etc.

The request sets ``stream: true`` and the provider parses the Server-Sent
Events response token-by-token, yielding one :class:`Chunk` per content
delta. A terminal chunk carries the ``finish_reason`` and (when the server
honours ``stream_options.include_usage``) the cumulative token count.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

import httpx

from dagagent.providers.base import Chunk, Message, ProviderUnavailableError


class _EventFields(NamedTuple):
    """The pieces of one SSE event the provider cares about."""

    content: str | None
    finish_reason: str | None
    tool_name: str | None
    tool_args: str
    tokens: int | None


class OpenAICompatProvider:
    """A single tier backed by an OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        *,
        name: str,
        tier: int,
        base_url: str,
        model: str,
        api_key: str = "",
        default_timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._name = name
        self._tier = tier
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._default_timeout_s = default_timeout_s
        # Injectable for tests (httpx.MockTransport). None → httpx default.
        self._transport = transport

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> int:
        return self._tier

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        body = self._build_body(messages, json_mode=json_mode, max_tokens=max_tokens)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        effective_timeout = timeout_s if timeout_s is not None else self._default_timeout_s
        url = f"{self._base_url}/chat/completions"

        # Accumulated across deltas: tool calls arrive fragmented, and the
        # finish_reason / usage land on separate trailing events.
        emitted_content = False
        tool_call_name: str | None = None
        tool_call_args = ""
        finish_reason: str | None = None
        tokens: int | None = None

        try:
            async with (
                httpx.AsyncClient(timeout=effective_timeout, transport=self._transport) as client,
                client.stream("POST", url, headers=headers, json=body) as resp,
            ):
                if resp.status_code >= 400:
                    await resp.aread()
                    resp.raise_for_status()

                async for line in resp.aiter_lines():
                    event = _decode_sse_line(line)
                    if event is None:
                        continue
                    fields = _parse_event(event)
                    if fields.content:
                        emitted_content = True
                        yield Chunk(text=fields.content)
                    if fields.finish_reason is not None:
                        finish_reason = fields.finish_reason
                    if fields.tool_name is not None:
                        tool_call_name = fields.tool_name
                    tool_call_args += fields.tool_args
                    if fields.tokens is not None:
                        tokens = fields.tokens
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise ProviderUnavailableError(f"{self._name}: {type(exc).__name__}: {exc}") from exc

        tool_content = ""
        if not emitted_content and tool_call_name is not None:
            tool_content = _synthesise_tool_content(tool_call_name, tool_call_args)

        if tool_content or finish_reason is not None or tokens is not None:
            yield Chunk(text=tool_content, finish_reason=finish_reason, tokens_used=tokens)

    def _build_body(
        self,
        messages: list[Message],
        *,
        json_mode: bool,
        max_tokens: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "stream": True,
            # Ask for usage in the final SSE event. Servers that don't grok
            # this field generally ignore it; token counts simply stay None.
            "stream_options": {"include_usage": True},
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body


def _decode_sse_line(line: str) -> Any | None:
    """Parse one SSE ``data:`` line into a JSON object, or ``None`` to skip.

    Comments, blank lines, the terminal ``[DONE]`` sentinel, and any
    malformed JSON are all treated as skippable.
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _parse_event(event: Any) -> _EventFields:
    """Extract content / finish_reason / tool-call / usage from one event."""
    content: str | None = None
    finish_reason: str | None = None
    tool_name: str | None = None
    tool_args = ""

    choices: Any = event.get("choices") or []
    if choices:
        choice0: Any = choices[0]
        delta: Any = choice0.get("delta") or {}

        raw_content = delta.get("content")
        if isinstance(raw_content, str) and raw_content:
            content = raw_content

        raw_reason = choice0.get("finish_reason")
        if isinstance(raw_reason, str):
            finish_reason = raw_reason

        tool_calls: Any = delta.get("tool_calls") or []
        for call in tool_calls:
            fn: Any = call.get("function") or {}
            fn_name = fn.get("name")
            if isinstance(fn_name, str):
                tool_name = fn_name
            fragment = fn.get("arguments")
            if isinstance(fragment, str):
                tool_args += fragment

    tokens: int | None = None
    usage: Any = event.get("usage")
    if usage:
        total = usage.get("total_tokens")
        if isinstance(total, int):
            tokens = total

    return _EventFields(content, finish_reason, tool_name, tool_args, tokens)


def _synthesise_tool_content(name: str, raw_args: str) -> str:
    """Mirror a streamed tool call into JSON content the planner can parse.

    Some OpenAI-compatible APIs return the plan as a tool call rather than
    in ``content``; we flatten it so downstream JSON parsing is uniform.
    """
    try:
        arguments: Any = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        arguments = raw_args
    return json.dumps({"tool_call": name, "arguments": arguments})
