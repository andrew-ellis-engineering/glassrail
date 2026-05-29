"""Unit tests for the tracing setup helpers.

These avoid installing a global tracer provider (that's covered by the
integration trace-tree test) — they exercise the parts that have no global
side effects: the disabled fast-path and the provider-model lookup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from dagagent.config import Settings
from dagagent.providers import Chunk, Message, OpenAICompatProvider
from dagagent.telemetry import configure_tracing, get_tracer, provider_model


def test_configure_tracing_disabled_is_noop() -> None:
    # Default settings: tracing off, no endpoint, no exporter.
    assert configure_tracing(Settings()) is False


def test_get_tracer_returns_a_tracer() -> None:
    tracer = get_tracer()
    # Creating a span must never raise, even with no provider installed.
    with tracer.start_as_current_span("probe") as span:
        span.set_attribute("k", "v")


def test_provider_model_reads_openai_compat_model() -> None:
    provider = OpenAICompatProvider(
        name="tier0", tier=0, base_url="http://localhost/v1", model="my-model-x"
    )
    assert provider_model(provider) == "my-model-x"


class _ModellessProvider:
    """A protocol-satisfying provider that exposes no model attribute."""

    @property
    def name(self) -> str:
        return "modelless"

    @property
    def tier(self) -> int:
        return 0

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        del messages, json_mode, max_tokens, timeout_s
        yield Chunk(text="")


def test_provider_model_none_when_absent() -> None:
    assert provider_model(_ModellessProvider()) is None
