"""OpenAI-compatible chat-completions provider.

Talks to any service exposing ``POST /chat/completions`` with OpenAI's
request/response shape — OpenAI, OpenRouter, Anthropic-through-OpenRouter,
local MLX through Rapid-MLX, Ollama, etc.

Phase 0.5 ships the non-streaming code path that returns a single chunk
containing the full response. Real token-by-token streaming will land in
a follow-up commit; the API surface is already streaming-shaped so callers
don't change.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from dagagent.providers.base import Chunk, Message, ProviderUnavailableError


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
    ) -> None:
        self._name = name
        self._tier = tier
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._default_timeout_s = default_timeout_s

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
        body: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        effective_timeout = timeout_s if timeout_s is not None else self._default_timeout_s

        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise ProviderUnavailableError(f"{self._name}: {type(exc).__name__}: {exc}") from exc

        choice = data["choices"][0]
        content = choice["message"].get("content") or ""

        # Some OpenAI-compatible APIs (MLX, OpenRouter) return tool_calls in
        # the message instead of putting them in content. Mirror them into
        # content so the planner JSON-parses cleanly.
        if not content and choice["message"].get("tool_calls"):
            tc = choice["message"]["tool_calls"][0]
            content = json.dumps(
                {
                    "tool_call": tc["function"]["name"],
                    "arguments": json.loads(tc["function"].get("arguments", "{}")),
                }
            )

        usage = cast("dict[str, Any]", data.get("usage") or {})
        total = usage.get("total_tokens")
        tokens = total if isinstance(total, int) else None

        finish_reason = choice.get("finish_reason")
        yield Chunk(text=content, finish_reason=finish_reason, tokens_used=tokens)
