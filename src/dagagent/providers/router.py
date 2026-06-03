"""Tier router — walks an ordered provider list, falls through on unavailable.

The router exposes the same streaming ``complete()`` shape as a single
provider, so call sites don't care whether they're talking to a router or
to a bare provider.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence

from opentelemetry.trace import Status, StatusCode

from dagagent.providers.base import (
    Chunk,
    LLMProvider,
    Message,
    ProviderError,
    ProviderUnavailableError,
)
from dagagent.telemetry import (
    ATTR_GEN_AI_OPERATION,
    ATTR_GEN_AI_REQUEST_MODEL,
    ATTR_GEN_AI_SYSTEM,
    ATTR_GEN_AI_USAGE_TOTAL_TOKENS,
    ATTR_MIN_TIER,
    ATTR_TIER,
    LLM_SPAN_KIND,
    SPAN_LLM,
    get_tracer,
    provider_model,
)

log = logging.getLogger(__name__)


class TierRouter:
    """Ordered list of providers with timeout-fallthrough."""

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        *,
        max_generation_tokens: int | None = None,
    ):
        if not providers:
            raise ValueError("TierRouter requires at least one provider")
        self._providers: list[LLMProvider] = list(providers)
        self._max_generation_tokens = max_generation_tokens

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

        if self._max_generation_tokens is not None and max_tokens > self._max_generation_tokens:
            log.warning(
                "max_tokens %d exceeds generation ceiling %d; clamping",
                max_tokens,
                self._max_generation_tokens,
            )
            max_tokens = self._max_generation_tokens

        # A leaf span over the whole call — started but not made "current", so
        # it nests under the active node/plan span without holding the context
        # open across the generator's yields.
        span = get_tracer().start_span(SPAN_LLM, kind=LLM_SPAN_KIND)
        span.set_attribute(ATTR_GEN_AI_OPERATION, "chat")
        span.set_attribute(ATTR_MIN_TIER, min_tier)
        last_error: Exception | None = None
        try:
            for provider in eligible:
                # Fast pre-flight check: if the provider exposes is_healthy(),
                # call it with a short timeout before attempting generation.
                # This lets the router skip a dead local server in ~3 seconds
                # instead of waiting for the full generation timeout.
                if hasattr(provider, "is_healthy") and not await provider.is_healthy():  # type: ignore[union-attr]
                    log.warning(
                        "Provider %s (tier %d) failed health check, skipping",
                        provider.name,
                        provider.tier,
                    )
                    last_error = ProviderUnavailableError(
                        f"{provider.name} (tier {provider.tier}): health check failed"
                    )
                    continue

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

                # Committed to this provider; record who served the request.
                span.set_attribute(ATTR_GEN_AI_SYSTEM, provider.name)
                span.set_attribute(ATTR_TIER, provider.tier)
                model = provider_model(provider)
                if model is not None:
                    span.set_attribute(ATTR_GEN_AI_REQUEST_MODEL, model)

                total_tokens = first.tokens_used or 0
                yield first
                async for chunk in stream:
                    if chunk.tokens_used is not None:
                        total_tokens = chunk.tokens_used
                    yield chunk
                if total_tokens:
                    span.set_attribute(ATTR_GEN_AI_USAGE_TOTAL_TOKENS, total_tokens)
                span.set_status(Status(StatusCode.OK))
                return

            span.set_status(Status(StatusCode.ERROR, "all providers unavailable"))
            raise ProviderUnavailableError(f"All providers exhausted; last error: {last_error}")
        finally:
            span.end()
