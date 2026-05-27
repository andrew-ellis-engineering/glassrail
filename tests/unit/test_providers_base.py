"""Tests for the provider base types and the ``collect`` helper."""

from __future__ import annotations

from collections.abc import AsyncIterator

from dagagent.providers import Chunk, LLMProvider, Message, collect


async def _stream(chunks: list[Chunk]) -> AsyncIterator[Chunk]:
    for c in chunks:
        yield c


async def test_collect_joins_text_and_takes_last_token_count() -> None:
    chunks = [
        Chunk(text="hello "),
        Chunk(text="world", tokens_used=12),
    ]
    text, tokens = await collect(_stream(chunks))
    assert text == "hello world"
    assert tokens == 12


async def test_collect_empty_stream() -> None:
    text, tokens = await collect(_stream([]))
    assert text == ""
    assert tokens == 0


async def test_collect_no_token_reports() -> None:
    chunks = [Chunk(text="x"), Chunk(text="y")]
    text, tokens = await collect(_stream(chunks))
    assert text == "xy"
    assert tokens == 0


class _FakeProvider:
    def __init__(self, *, name: str = "fake", tier: int = 0) -> None:
        self._name = name
        self._tier = tier

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
        yield Chunk(text="ok", tokens_used=1)


def test_protocol_is_runtime_checkable() -> None:
    provider = _FakeProvider()
    assert isinstance(provider, LLMProvider)
