"""Build providers and routers from :class:`Settings`.

Keeps the wiring logic in one place so the gateway and CLI don't each
hand-construct providers.
"""

from __future__ import annotations

from dagagent.config import Settings, TierConfig
from dagagent.providers.openai_compat import OpenAICompatProvider
from dagagent.providers.router import TierRouter


def _provider_from_tier(tier: int, cfg: TierConfig) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name=f"tier{tier}",
        tier=tier,
        base_url=cfg.base_url,
        model=cfg.model,
        api_key=cfg.api_key,
        default_timeout_s=cfg.timeout_s,
    )


def router_from_settings(settings: Settings) -> TierRouter:
    """Construct a :class:`TierRouter` from the configured tier list."""
    providers = [_provider_from_tier(i, cfg) for i, cfg in enumerate(settings.tiers)]
    return TierRouter(providers)
