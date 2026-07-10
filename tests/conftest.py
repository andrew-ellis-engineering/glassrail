"""Project-wide pytest fixtures.

Subpackage-specific fixtures live in their own conftest.py. Anything needed
by more than one subtree (event bus, ULID seeding, etc.) belongs here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from copy import deepcopy

import pytest

from glassrail.providers import Chunk, Message


class ScriptedProvider:
    """Fake provider that pops scripted responses in order."""

    def __init__(
        self,
        responses: Sequence[str | Exception],
        *,
        tier: int = 0,
        name: str = "scripted",
        model: str = "scripted-model",
        tokens_used: int = 1,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> None:
        self._responses: list[str | Exception] = list(responses)
        self._tier = tier
        self._name = name
        self._model = model
        self._tokens_used = tokens_used
        self._cache_read_tokens = cache_read_tokens
        self._cache_write_tokens = cache_write_tokens
        self.max_tokens_seen: list[int] = []
        self.system_seen: list[str] = []
        self.user_seen: list[str] = []
        self.messages_seen: list[list[Message]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def model(self) -> str:
        return self._model

    @property
    def user_messages(self) -> list[str]:
        """Compatibility alias for older integration assertions."""
        return self.user_seen

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        del json_mode, timeout_s
        self.max_tokens_seen.append(max_tokens)
        self.messages_seen.append(deepcopy(messages))
        self.system_seen.append(next((m["content"] for m in messages if m["role"] == "system"), ""))
        self.user_seen.append(next((m["content"] for m in messages if m["role"] == "user"), ""))
        if not self._responses:
            raise RuntimeError("scripted exhausted")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        yield Chunk(
            text=response,
            tokens_used=self._tokens_used,
            cache_read_tokens=self._cache_read_tokens,
            cache_write_tokens=self._cache_write_tokens,
        )


def make_scripted(
    responses: Sequence[str | Exception],
    *,
    tier: int = 0,
    name: str = "scripted",
    model: str = "scripted-model",
    tokens_used: int = 1,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> ScriptedProvider:
    return ScriptedProvider(
        responses,
        tier=tier,
        name=name,
        model=model,
        tokens_used=tokens_used,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def make_capturing_scripted(
    responses: Sequence[str | Exception],
    *,
    tier: int = 0,
    name: str = "capturing",
    model: str = "scripted-model",
    tokens_used: int = 1,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> ScriptedProvider:
    return make_scripted(
        responses,
        tier=tier,
        name=name,
        model=model,
        tokens_used=tokens_used,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate tests from the user's persistent config files.

    config.toml (CWD) sets planner_min_tier=1 and points tiers at local
    servers; ~/.glassrail/config.toml does the same. Tests use scripted
    providers at tier 0, so we reset both settings here rather than have
    every Settings() construction deal with production overrides.
    """
    monkeypatch.setenv("GLASSRAIL_PLANNER_MIN_TIER", "0")
    # Point the home config directory at a non-existent path so tests
    # don't pick up ~/.glassrail/config.toml.
    monkeypatch.setenv("GLASSRAIL_CONFIG_HOME", "/nonexistent/glassrail-test")
