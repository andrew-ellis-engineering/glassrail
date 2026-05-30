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

from dagagent.config import prompts as _prompts


class TierConfig(BaseModel):
    """Configuration for a single LLM tier.

    A tier is one entry in the ordered list the :class:`TierRouter` walks on
    fallthrough. Order in the parent :class:`Settings` is the routing order.
    """

    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 60.0


class NodeBudgets(BaseModel):
    """Per-node output-token budgets — the ``max_tokens`` (generation) cap on
    each LLM call the agent makes.

    These cap *output*, not input. Every node runs with a fresh context, so a
    budget sets how much room a node has to do its job: reasoning and summaries
    need real room, while structured micro-calls (a branch label, an args
    object, a yes/no gate) need very little. The model's *input* capacity is a
    separate concern, bounded by the served model's context window — not set
    here. Override any field via ``config.toml`` ``[budgets]`` or
    ``DAGAGENT_BUDGETS__<FIELD>``.
    """

    planner: int = 4096
    """The full plan JSON."""
    think: int = 8192
    """Multi-step reasoning over prior context."""
    summary: int = 8192
    """High-fidelity summaries of documents and webpages."""
    synthesis: int = 4096
    """Combine prior node outputs into a response."""
    result: int = 4096
    """The final user-facing answer."""
    decision: int = 256
    """A branch label + confidence — structured micro-call."""
    extract_args: int = 512
    """A tool-args JSON object — structured micro-call."""
    shape_check: int = 128
    """A yes/no output-shape gate — structured micro-call."""


class NodePrompts(BaseModel):
    """System prompts for each node role.

    The planner and executor read these instead of hard-coding prompt text, so
    you can tune a node's behaviour without editing source. Defaults live in
    :mod:`dagagent.config.prompts`. Override any field under ``[prompts]`` in
    ``config.toml`` or ``DAGAGENT_PROMPTS__<FIELD>``. Each prompt must keep
    instructing the model to emit the JSON shape its node expects.
    """

    planner: str = _prompts.DEFAULT_PLANNER_SYSTEM
    """Plan generation — must request the plan JSON schema."""
    decision: str = _prompts.DEFAULT_DECISION_SYSTEM
    """Binary branch evaluation — must request {branch, confidence}."""
    think: str = _prompts.DEFAULT_THINK_SYSTEM
    """Multi-step reasoning — must request {reasoning, confidence}."""
    synthesis: str = _prompts.DEFAULT_SYNTHESIS_SYSTEM
    """Combine prior outputs — must request {output, confidence}."""
    summary: str = _prompts.DEFAULT_SUMMARY_SYSTEM
    """Condense upstream context — must request {summary, confidence}."""
    result: str = _prompts.DEFAULT_RESULT_SYSTEM
    """The final answer — must request {output, confidence}."""
    shape_check: str = _prompts.DEFAULT_SHAPE_CHECK_SYSTEM
    """Tool-output gate — must request {matches_expectation, issue}."""


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
    max_replan_attempts: int = 1
    confidence_threshold: float = 0.75
    max_subplan_nodes: int = 12
    max_subplans_per_plan: int = 2

    # ── Per-node output-token budgets ────────────────────────────────────
    budgets: NodeBudgets = NodeBudgets()

    # ── Per-node system prompts ──────────────────────────────────────────
    prompts: NodePrompts = NodePrompts()

    # ── Tools ────────────────────────────────────────────────────────────
    # Built-in tools always register. Third-party tools advertised through the
    # ``dagagent.tools`` entry-point group are opt-in: discovering whatever is
    # installed is a deliberate choice, not a default.
    load_tool_plugins: bool = False

    # ── HITL ─────────────────────────────────────────────────────────────
    confirm_plans: bool = False

    # ── Observability ────────────────────────────────────────────────────
    # Tracing is a no-op unless turned on here. Setting an OTLP endpoint
    # implies enabling it. See dagagent.telemetry.configure_tracing.
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    """OTLP/HTTP traces endpoint, e.g. http://localhost:4318/v1/traces."""
    otel_service_name: str = "dagagent"
    otel_console_export: bool = False
    """Also print spans to stdout — handy for local debugging."""

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
