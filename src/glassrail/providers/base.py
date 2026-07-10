"""LLM provider protocol and shared types.

Providers expose a single streaming method (``complete``). Non-streaming
callers use the :func:`collect` helper to drain the iterator into a string.
The :class:`TierRouter` (see :mod:`glassrail.providers.router`) walks an
ordered list of providers and falls through to the next on
``ProviderUnavailableError``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, NotRequired, Protocol, TypedDict, runtime_checkable

from pydantic import BaseModel

from glassrail.core import GlassrailError


class Message(TypedDict):
    """A chat message plus provider-neutral prompt-cache metadata.

    ``cache_prefix_chars`` marks the reusable prefix ending at that character
    offset. Providers that support explicit caching translate it to their wire
    format; providers that do not must ignore it and send ``content`` unchanged.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    cache_prefix_chars: NotRequired[int]


def cacheable_message(
    role: Literal["system", "user", "assistant", "tool"],
    content: str,
    *,
    prefix_chars: int | None = None,
) -> Message:
    """Build a message whose stable prefix is eligible for provider caching."""
    message: Message = {"role": role, "content": content}
    if not content:
        return message
    breakpoint = len(content) if prefix_chars is None else prefix_chars
    if breakpoint <= 0 or breakpoint > len(content):
        raise ValueError("cache prefix must end within non-empty message content")
    message["cache_prefix_chars"] = breakpoint
    return message


class Chunk(BaseModel):
    """One streaming output fragment from a provider."""

    text: str = ""
    finish_reason: str | None = None
    tokens_used: int | None = None
    """Cumulative token count for the response, if the provider reports it."""
    cache_read_tokens: int | None = None
    """Prompt tokens served from provider cache, if reported."""
    cache_write_tokens: int | None = None
    """Prompt tokens written to provider cache, if reported."""


class ProviderError(GlassrailError):
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
