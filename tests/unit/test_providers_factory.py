"""Tests for the provider factory."""

from __future__ import annotations

from glassrail.config import Settings, TierConfig
from glassrail.providers import OpenAICompatProvider, TierRouter, router_from_settings


def test_router_from_settings_builds_one_provider_per_tier() -> None:
    settings = Settings(
        tier0=TierConfig(base_url="http://t0", model="m0", timeout_s=5.0),
        tier1=TierConfig(base_url="http://t1", model="m1", api_key="k1"),
        tier2=TierConfig(base_url="http://t2", model="m2"),
        tier3=TierConfig(base_url="http://t3", model="m3"),
    )
    router = router_from_settings(settings)
    assert isinstance(router, TierRouter)
    providers = router.providers
    assert len(providers) == 4
    assert all(isinstance(p, OpenAICompatProvider) for p in providers)
    assert [p.tier for p in providers] == [0, 1, 2, 3]
    assert [p.name for p in providers] == ["tier0", "tier1", "tier2", "tier3"]
