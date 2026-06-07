"""Scripted provider — replays canned JSONL responses in call order.

Promoted from the ``_Scripted`` test helper pattern to a real provider so
the harness-mechanics eval suite can run without a live model.  Each line
of the JSONL file is the raw text the executor will receive for the next
LLM call, consumed in strict order.  Responses are loaded once at
construction; a fresh process per trial means the deque starts full every
time.

Configure via tier env vars:

    GLASSRAIL_TIER0__KIND=scripted
    GLASSRAIL_TIER0__SCRIPTED_PATH=/abs/path/to/responses.jsonl
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

from glassrail.providers.base import Chunk, Message, ProviderError


class ScriptedProvider:
    """Replays a JSONL file of canned model responses, one per LLM call."""

    def __init__(self, *, name: str, tier: int, path: str) -> None:
        raw = Path(path).read_text(encoding="utf-8")
        self._name = name
        self._tier = tier
        self._responses: deque[str] = deque(line for line in raw.splitlines() if line.strip())

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
        if not self._responses:
            raise ProviderError(
                f"ScriptedProvider '{self._name}' exhausted: "
                "more LLM calls were made than there are lines in the JSONL fixture"
            )
        text = self._responses.popleft()
        yield Chunk(text=text, finish_reason="stop", tokens_used=0)
