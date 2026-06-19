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
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

import httpx

from glassrail.providers.base import Chunk, Message, ProviderError, ProviderUnavailableError


class _EventFields(NamedTuple):
    """The pieces of one SSE event the provider cares about."""

    content: str | None
    reasoning: str | None
    finish_reason: str | None
    tool_name: str | None
    tool_args: str
    tokens: int | None


@dataclass
class _StreamState:
    """Mutable accumulator for one streamed completion attempt."""

    emitted_content: bool = False
    saw_reasoning: bool = False
    tool_call_name: str | None = None
    tool_call_args: str = ""
    finish_reason: str | None = None
    tokens: int | None = None

    def apply(self, fields: _EventFields) -> Chunk | None:
        if fields.finish_reason is not None:
            self.finish_reason = fields.finish_reason
        self.saw_reasoning = self.saw_reasoning or bool(fields.reasoning)
        if fields.tool_name is not None:
            self.tool_call_name = fields.tool_name
        self.tool_call_args += fields.tool_args
        if fields.tokens is not None:
            self.tokens = fields.tokens
        if not fields.content:
            return None
        self.emitted_content = True
        return Chunk(text=fields.content)


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
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._name = name
        self._tier = tier
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._default_timeout_s = default_timeout_s
        self._extra_body: dict[str, Any] = extra_body or {}
        # Injectable for tests (httpx.MockTransport). None → httpx default.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=None, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        """Close the persistent HTTP client, if it has been opened."""
        if self._client is not None:
            await self._client.aclose()

    async def is_healthy(self, timeout_s: float = 3.0) -> bool:
        """Return True if the endpoint is reachable and reports healthy.

        Tries ``GET /health`` (rapid-mlx / most local servers) with a short
        timeout so the router can skip a dead tier in seconds rather than
        waiting for the full generation timeout.  Falls back to True on any
        unexpected response shape so as not to block non-local providers that
        don't expose /health.
        """
        # Derive the server root from base_url — strip any /v1 suffix so
        # /v1/health doesn't accidentally become /v1/v1/health.
        root = self._base_url
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        url = f"{root}/health"
        try:
            resp = await self._get_client().get(url, timeout=timeout_s)
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    return body.get("status") == "healthy"
                except Exception:
                    # Non-JSON 200 (e.g. an HTML landing page from a cloud
                    # provider that doesn't expose /health) — assume available.
                    return True
            # 404 → server doesn't implement /health; assume available.
            return resp.status_code == 404
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

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
        attempted_reasoning_retry = False

        while True:
            state = _StreamState()

            try:
                async with self._get_client().stream(
                    "POST",
                    url,
                    headers=headers,
                    json=body,
                    timeout=effective_timeout,
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        retry_body = _reasoning_retry_body(
                            resp,
                            body=body,
                            already_retried=attempted_reasoning_retry,
                        )
                        if retry_body is not None:
                            body = retry_body
                            attempted_reasoning_retry = True
                            continue
                        self._raise_for_status(resp)

                    async for line in resp.aiter_lines():
                        event = _decode_sse_line(line)
                        if event is None:
                            continue
                        chunk = state.apply(_parse_event(event))
                        if chunk is not None:
                            yield chunk
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                raise ProviderUnavailableError(
                    f"{self._name}: {type(exc).__name__}: {exc}"
                ) from exc
            break

        # If the model returned reasoning tokens but no content, the reasoning
        # parameter was not honoured by the underlying provider. Surface a clear
        # error instead of letting the caller receive an empty string and produce
        # a confusing JSON parse failure downstream.
        if not state.emitted_content and state.saw_reasoning and state.tool_call_name is None:
            raise ProviderError(
                f"{self._name}: model returned only reasoning tokens with no content. "
                "Reasoning is not disabled on this provider. "
                "Set reasoning.effort=none with provider.require_parameters=true "
                "in extra_body to ensure OpenRouter routes only to compliant providers."
            )

        tool_content = ""
        if not state.emitted_content and state.tool_call_name is not None:
            tool_content = _synthesise_tool_content(state.tool_call_name, state.tool_call_args)

        if tool_content or state.finish_reason is not None or state.tokens is not None:
            yield Chunk(
                text=tool_content,
                finish_reason=state.finish_reason,
                tokens_used=state.tokens,
            )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """Translate HTTP error codes into typed provider exceptions.

        Auth failures, rate limits, and server errors fall through to the next
        tier (ProviderUnavailableError). Malformed-request errors (400/422)
        propagate — retrying a different tier with the same body won't help.
        """
        if resp.status_code in (401, 403, 404, 429):
            raise ProviderUnavailableError(f"{self._name}: HTTP {resp.status_code}")
        if resp.status_code in (400, 422):
            raise ProviderError(f"{self._name}: HTTP {resp.status_code}: {resp.text}")
        if resp.status_code >= 500:
            # Server errors (including 503 during a watchdog-triggered restart)
            # are tier-level problems — fall through to the next tier.
            raise ProviderUnavailableError(f"{self._name}: HTTP {resp.status_code}")
        resp.raise_for_status()

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
        if self._extra_body:
            body.update(self._extra_body)
        return body


def _reasoning_retry_body(
    resp: httpx.Response,
    *,
    body: dict[str, Any],
    already_retried: bool,
) -> dict[str, Any] | None:
    if already_retried or not _requires_reasoning(resp):
        return None
    return _without_disabled_reasoning(body)


def _requires_reasoning(resp: httpx.Response) -> bool:
    body = resp.text.lower()
    return "reasoning is mandatory" in body and "cannot be disabled" in body


def _without_disabled_reasoning(body: dict[str, Any]) -> dict[str, Any] | None:
    reasoning = body.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    reasoning_body = cast("dict[str, Any]", reasoning)
    disabled = reasoning_body.get("effort") == "none" or reasoning_body.get("enabled") is False
    if not disabled:
        return None
    retry_body = dict(body)
    retry_body.pop("reasoning", None)
    return retry_body


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
    """Extract content / reasoning / finish_reason / tool-call / usage from one event."""
    content: str | None = None
    reasoning: str | None = None
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

        # OpenRouter streams reasoning tokens in delta.reasoning (not delta.content).
        # We track their presence to detect reasoning-only responses and raise a
        # clear error rather than silently returning empty content.
        raw_reasoning = delta.get("reasoning")
        if isinstance(raw_reasoning, str) and raw_reasoning:
            reasoning = raw_reasoning

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

    return _EventFields(content, reasoning, finish_reason, tool_name, tool_args, tokens)


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
