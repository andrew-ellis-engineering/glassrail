"""Configuration via pydantic-settings.

Settings are loaded with the following precedence (highest first):

1. Values passed to ``Settings(...)`` directly (used in tests).
2. Environment variables prefixed ``DAGAGENT_``.
3. A ``.env`` file in the current working directory.
4. A ``config.toml`` file in the current working directory.
5. Defaults declared on the model.

Nested fields use the double-underscore delimiter, e.g.
``DAGAGENT_TIER0__MODEL=anthropic/claude-sonnet-4-6``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class TierConfig(BaseModel):
    """Configuration for a single LLM tier.

    A tier is one entry in the ordered list the :class:`TierRouter` walks on
    fallthrough. Order in the parent :class:`Settings` is the routing order.
    """

    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 60.0


_DEFAULT_TIER0 = TierConfig(
    base_url="http://localhost:8080/v1",
    model="qwen3.6-35b-moe",
    timeout_s=10.0,
)
_DEFAULT_TIER1 = TierConfig(
    base_url="https://openrouter.ai/api/v1",
    model="deepseek/deepseek-v4-flash",
)
_DEFAULT_TIER2 = TierConfig(
    base_url="https://openrouter.ai/api/v1",
    model="deepseek/deepseek-v4-pro",
)
_DEFAULT_TIER3 = TierConfig(
    base_url="https://openrouter.ai/api/v1",
    model="anthropic/claude-sonnet-4-6",
)


class Settings(BaseSettings):
    """Process-wide configuration."""

    model_config = SettingsConfigDict(
        env_prefix="DAGAGENT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file="config.toml",
        extra="ignore",
        # Without this, partial env/TOML overrides on tier* would erase the
        # rest of the TierConfig defaults instead of merging with them.
        nested_model_default_partial_update=True,
    )

    # ── Persistence ──────────────────────────────────────────────────────
    state_path: Path = Path("./state.sqlite")

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = False

    # ── Tiers ────────────────────────────────────────────────────────────
    # Direct (not factory) defaults so pydantic-settings can deep-merge
    # partial env / TOML overrides under nested_model_default_partial_update.
    tier0: TierConfig = _DEFAULT_TIER0
    tier1: TierConfig = _DEFAULT_TIER1
    tier2: TierConfig = _DEFAULT_TIER2
    tier3: TierConfig = _DEFAULT_TIER3

    # ── Plan limits ──────────────────────────────────────────────────────
    max_plan_nodes: int = 12
    max_decision_nesting_depth: int = 2
    max_node_output_tokens: int = 2000
    max_replan_attempts: int = 1
    confidence_threshold: float = 0.75
    max_subplan_nodes: int = 12
    max_subplans_per_plan: int = 2

    # ── HITL ─────────────────────────────────────────────────────────────
    confirm_plans: bool = False

    @property
    def tiers(self) -> list[TierConfig]:
        """Ordered list of configured tiers — index matches tier number."""
        return [self.tier0, self.tier1, self.tier2, self.tier3]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )
