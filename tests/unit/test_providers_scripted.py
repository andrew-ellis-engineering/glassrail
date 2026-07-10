"""Tests for the JSONL scripted provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from glassrail.providers import (
    Message,
    ProviderError,
    ProviderUnavailableError,
    ScriptedProvider,
    collect,
)

_MSG: list[Message] = [{"role": "user", "content": "hi"}]


async def test_scripted_provider_replays_jsonl_lines(tmp_path: Path) -> None:
    path = tmp_path / "responses.jsonl"
    path.write_text('{"output": "one"}\n{"output": "two"}\n', encoding="utf-8")
    provider = ScriptedProvider(name="scripted", tier=0, path=str(path))

    first, _ = await collect(provider.complete(_MSG))
    second, _ = await collect(provider.complete(_MSG))

    assert first == '{"output": "one"}'
    assert second == '{"output": "two"}'


async def test_scripted_provider_unavailable_directive(tmp_path: Path) -> None:
    path = tmp_path / "responses.jsonl"
    path.write_text('{"__error__": "provider_unavailable"}\n', encoding="utf-8")
    provider = ScriptedProvider(name="scripted", tier=0, path=str(path))

    with pytest.raises(ProviderUnavailableError):
        await collect(provider.complete(_MSG))


async def test_scripted_provider_error_directive(tmp_path: Path) -> None:
    path = tmp_path / "responses.jsonl"
    path.write_text('{"__error__": "provider"}\n', encoding="utf-8")
    provider = ScriptedProvider(name="scripted", tier=0, path=str(path))

    with pytest.raises(ProviderError):
        await collect(provider.complete(_MSG))
