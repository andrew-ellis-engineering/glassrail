"""LLM provider protocol and shared types.

Providers expose a single streaming method (``complete``). Non-streaming
callers use the :func:`collect` helper to drain the iterator into a string.
The :class:`TierRouter` (see :mod:`dagagent.providers.router`) walks an
ordered list of providers and falls through to the next on
``ProviderUnavailableError``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol, TypedDict, runtime_checkable

from pydantic import BaseModel

from dagagent.core import DagagentError


class Message(TypedDict):
    """An OpenAI-compatible chat message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class Chunk(BaseModel):
    """One streaming output fragment from a provider."""

    text: str = ""
    finish_reason: str | None = None
    tokens_used: int | None = None
    """Cumulative token count for the response, if the provider reports it."""


class ProviderError(DagagentError):
    """Base for provider-related errors."""


class ProviderUnavailableError(ProviderError):
    """Raised when a provider cannot serve the request (timeout, connect failure).

    The :class:`TierRouter` treats this as a signal to fall through to the
    next tier. Other exceptions propagate unchanged.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """The interface every LLM backend implements."""

    @property
    def name(self) -> str:
        """Human-readable provider name, used in logs."""
        ...

    @property
    def tier(self) -> int:
        """Tier number this provider serves. Lower = preferred."""
        ...

    def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        """Stream a completion. Implementations are ``async def`` generators."""
        ...


async def collect(stream: AsyncIterator[Chunk]) -> tuple[str, int]:
    """Drain a chunk stream into ``(text, total_tokens)``.

    Use this when the caller does not care about streaming — most plan
    generation and synthesis calls fall in that bucket.
    """
    parts: list[str] = []
    tokens = 0
    async for chunk in stream:
        if chunk.text:
            parts.append(chunk.text)
        if chunk.tokens_used is not None:
            tokens = chunk.tokens_used
    return "".join(parts), tokens
