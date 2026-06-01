"""LLM provider abstraction.

- :mod:`dagagent.providers.base` defines the :class:`LLMProvider` Protocol
  and shared types.
- :mod:`dagagent.providers.router` defines :class:`TierRouter`, which walks
  an ordered provider list with timeout-fallthrough.
- :mod:`dagagent.providers.openai_compat` is the first concrete provider —
  one OpenAI-compatible HTTP endpoint per tier.
- :mod:`dagagent.providers.factory` wires :class:`Settings` to a router.
"""

from __future__ import annotations

from dagagent.providers.base import (
    Chunk,
    LLMProvider,
    Message,
    ProviderError,
    ProviderUnavailableError,
    collect,
)
from dagagent.providers.factory import router_from_settings
from dagagent.providers.openai_compat import OpenAICompatProvider
from dagagent.providers.postprocess import strip_model_output
from dagagent.providers.router import TierRouter

__all__ = [
    "Chunk",
    "LLMProvider",
    "Message",
    "OpenAICompatProvider",
    "ProviderError",
    "ProviderUnavailableError",
    "TierRouter",
    "collect",
    "router_from_settings",
    "strip_model_output",
]
