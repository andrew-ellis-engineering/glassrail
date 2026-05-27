"""Tier router — walks an ordered provider list, falls through on unavailable.

The router exposes the same streaming ``complete()`` shape as a single
provider, so call sites don't care whether they're talking to a router or
to a bare provider.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence

from dagagent.providers.base import (
    Chunk,
    LLMProvider,
    Message,
    ProviderError,
    ProviderUnavailableError,
)

log = logging.getLogger(__name__)


class TierRouter:
    """Ordered list of providers with timeout-fallthrough."""

    def __init__(self, providers: Sequence[LLMProvider]):
        if not providers:
            raise ValueError("TierRouter requires at least one provider")
        self._providers: list[LLMProvider] = list(providers)

    @property
    def providers(self) -> list[LLMProvider]:
        return list(self._providers)

    async def complete(
        self,
        messages: list[Message],
        *,
        min_tier: int = 0,
        max_tier: int | None = None,
        json_mode: bool = False,
        max_tokens: int = 1024,
    ) -> AsyncIterator[Chunk]:
        """Stream a completion, falling through to higher tiers on failure.

        Fallthrough happens BEFORE any chunk is emitted. Once a provider has
        produced output, the router commits to it; an error mid-stream
        propagates to the caller.
        """
        eligible = [
            p
            for p in self._providers
            if p.tier >= min_tier and (max_tier is None or p.tier <= max_tier)
        ]
        if not eligible:
            raise ProviderError(f"No providers configured for tier range [{min_tier}, {max_tier}]")

        last_error: Exception | None = None
        for provider in eligible:
            try:
                stream = provider.complete(
                    messages,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                )
                # Pull the first chunk inside the try so connect/timeout failures
                # at stream-open time count as "before any chunk emitted".
                first = await anext(stream, None)
            except ProviderUnavailableError as exc:
                log.warning(
                    "Provider %s (tier %d) unavailable: %s",
                    provider.name,
                    provider.tier,
                    exc,
                )
                last_error = exc
                continue

            if first is None:
                # Provider returned an empty stream — treat as unavailable.
                log.warning(
                    "Provider %s (tier %d) returned empty stream",
                    provider.name,
                    provider.tier,
                )
                continue

            yield first
            async for chunk in stream:
                yield chunk
            return

        raise ProviderUnavailableError(f"All providers exhausted; last error: {last_error}")
