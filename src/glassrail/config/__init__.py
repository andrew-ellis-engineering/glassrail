"""Configuration via pydantic-settings.

Loads from environment variables, an optional ``.env`` file, and an optional
``config.toml`` file. Twelve-factor by default; ``config.toml`` is provided
as an ergonomic override for self-hosters.
"""

from __future__ import annotations

from functools import lru_cache

from glassrail.config.settings import (
    ImageToolConfig,
    NodeBudgets,
    NodePrompts,
    RoutingConfig,
    Settings,
    TierConfig,
    ToolApprovalMode,
    ToolApprovalPolicy,
    ToolApprovalSettings,
    ToolsSettings,
    WebToolConfig,
)

__all__ = [
    "ImageToolConfig",
    "NodeBudgets",
    "NodePrompts",
    "RoutingConfig",
    "Settings",
    "TierConfig",
    "ToolApprovalMode",
    "ToolApprovalPolicy",
    "ToolApprovalSettings",
    "ToolsSettings",
    "WebToolConfig",
    "get_settings",
]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Use this from production code paths. Tests should construct ``Settings``
    directly with init kwargs instead of relying on this cache.
    """
    return Settings()
