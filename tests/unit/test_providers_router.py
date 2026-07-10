"""Tests for the TierRouter."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from glassrail.providers import (
    Chunk,
    Message,
    ProviderError,
    ProviderUnavailableError,
    TierRouter,
    collect,
)


class _Provider:
    """Fake provider for tests — emits configured chunks or raises."""

    def __init__(
        self,
        *,
        name: str,
        tier: int,
        chunks: list[Chunk] | None = None,
        raise_on_open: Exception | None = None,
        raise_mid_stream_after: int | None = None,
    ) -> None:
        self._name = name
        self._tier = tier
        self._chunks = chunks or []
        self._raise_on_open = raise_on_open
        self._raise_mid_stream_after = raise_mid_stream_after
        self.closed = False

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
        if self._raise_on_open is not None:
            raise self._raise_on_open
        for i, c in enumerate(self._chunks):
            if self._raise_mid_stream_after is not None and i >= self._raise_mid_stream_after:
                raise RuntimeError("mid-stream boom")
            yield c

    async def aclose(self) -> None:
        self.closed = True


_MSG: list[Message] = [{"role": "user", "content": "hi"}]


async def test_first_tier_serves_when_available() -> None:
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, chunks=[Chunk(text="from-0", tokens_used=1)]),
            _Provider(name="t1", tier=1, chunks=[Chunk(text="from-1", tokens_used=1)]),
        ]
    )
    text, _ = await collect(router.complete(_MSG))
    assert text == "from-0"


async def test_falls_through_on_provider_unavailable() -> None:
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, raise_on_open=ProviderUnavailableError("down")),
            _Provider(name="t1", tier=1, chunks=[Chunk(text="from-1", tokens_used=2)]),
        ]
    )
    text, tokens = await collect(router.complete(_MSG))
    assert text == "from-1"
    assert tokens == 2


async def test_falls_through_through_multiple_tiers() -> None:
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, raise_on_open=ProviderUnavailableError("0 down")),
            _Provider(name="t1", tier=1, raise_on_open=ProviderUnavailableError("1 down")),
            _Provider(name="t2", tier=2, chunks=[Chunk(text="from-2")]),
        ]
    )
    text, _ = await collect(router.complete(_MSG))
    assert text == "from-2"


async def test_all_tiers_exhausted_raises() -> None:
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, raise_on_open=ProviderUnavailableError("0 down")),
            _Provider(name="t1", tier=1, raise_on_open=ProviderUnavailableError("1 down")),
        ]
    )
    with pytest.raises(ProviderUnavailableError):
        await collect(router.complete(_MSG))


async def test_non_provider_error_does_not_trigger_fallthrough() -> None:
    """Errors other than ProviderUnavailableError propagate to the caller."""
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, raise_on_open=RuntimeError("bug")),
            _Provider(name="t1", tier=1, chunks=[Chunk(text="from-1")]),
        ]
    )
    with pytest.raises(RuntimeError, match="bug"):
        await collect(router.complete(_MSG))


async def test_mid_stream_error_propagates() -> None:
    """Once we've started yielding, errors are the caller's problem."""
    router = TierRouter(
        [
            _Provider(
                name="t0",
                tier=0,
                chunks=[Chunk(text="a"), Chunk(text="b")],
                raise_mid_stream_after=1,
            ),
            _Provider(name="t1", tier=1, chunks=[Chunk(text="from-1")]),
        ]
    )
    with pytest.raises(RuntimeError, match="boom"):
        await collect(router.complete(_MSG))


async def test_min_tier_skips_lower_tiers() -> None:
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, chunks=[Chunk(text="from-0")]),
            _Provider(name="t1", tier=1, chunks=[Chunk(text="from-1")]),
            _Provider(name="t2", tier=2, chunks=[Chunk(text="from-2")]),
        ]
    )
    text, _ = await collect(router.complete(_MSG, min_tier=2))
    assert text == "from-2"


async def test_max_tier_caps_upper_bound() -> None:
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, raise_on_open=ProviderUnavailableError("down")),
            _Provider(name="t1", tier=1, raise_on_open=ProviderUnavailableError("down")),
            _Provider(name="t2", tier=2, chunks=[Chunk(text="from-2")]),
        ]
    )
    with pytest.raises(ProviderUnavailableError):
        await collect(router.complete(_MSG, max_tier=1))


def test_empty_provider_list_raises() -> None:
    with pytest.raises(ValueError):
        TierRouter([])


async def test_no_eligible_providers_raises() -> None:
    router = TierRouter([_Provider(name="t0", tier=0, chunks=[Chunk(text="x")])])
    with pytest.raises(ProviderError):
        await collect(router.complete(_MSG, min_tier=5))


async def test_empty_stream_falls_through() -> None:
    router = TierRouter(
        [
            _Provider(name="t0", tier=0, chunks=[]),
            _Provider(name="t1", tier=1, chunks=[Chunk(text="from-1")]),
        ]
    )
    text, _ = await collect(router.complete(_MSG))
    assert text == "from-1"


async def test_aclose_closes_all_providers() -> None:
    first = _Provider(name="t0", tier=0)
    second = _Provider(name="t1", tier=1)
    router = TierRouter([first, second])

    await router.aclose()

    assert first.closed is True
    assert second.closed is True
