"""LLM provider abstraction.

- :mod:`glassrail.providers.base` defines the :class:`LLMProvider` Protocol
  and shared types.
- :mod:`glassrail.providers.router` defines :class:`TierRouter`, which walks
  an ordered provider list with timeout-fallthrough.
- :mod:`glassrail.providers.openai_compat` is the first concrete provider —
  one OpenAI-compatible HTTP endpoint per tier.
- :mod:`glassrail.providers.factory` wires :class:`Settings` to a router.
"""

from __future__ import annotations

from glassrail.providers.base import (
    Chunk,
    LLMProvider,
    Message,
    ProviderError,
    ProviderUnavailableError,
    collect,
)
from glassrail.providers.factory import router_from_settings
from glassrail.providers.openai_compat import OpenAICompatProvider
from glassrail.providers.postprocess import strip_model_output
from glassrail.providers.router import TierRouter
from glassrail.providers.scripted import ScriptedProvider

__all__ = [
    "Chunk",
    "LLMProvider",
    "Message",
    "OpenAICompatProvider",
    "ProviderError",
    "ProviderUnavailableError",
    "ScriptedProvider",
    "TierRouter",
    "collect",
    "router_from_settings",
    "strip_model_output",
]
