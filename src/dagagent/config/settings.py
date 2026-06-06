"""Configuration via pydantic-settings.

Settings are loaded with the following precedence (highest first):

1. Values passed to ``Settings(...)`` directly (used in tests).
2. Environment variables prefixed ``DAGAGENT_``.
3. A ``.env`` file in the current working directory.
4. A ``config.toml`` file in the current working directory (dev / project override).
5. ``~/.dagagent/config.toml`` — the persistent user config, always loaded
   regardless of working directory (used by the TUI and launchd services).
6. Defaults declared on the model.

Nested fields use the double-underscore delimiter, e.g.
``DAGAGENT_TIER0__MODEL=anthropic/claude-sonnet-4-6``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator
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

    kind: str = "openai_compat"
    """Provider kind: ``openai_compat`` (default) or ``scripted`` (eval-only)."""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout_s: float = 60.0
    scripted_path: str = ""
    """Absolute path to a JSONL responses file. Required when ``kind=scripted``."""

    @model_validator(mode="after")
    def _check_required_fields(self) -> TierConfig:
        if self.kind == "scripted":
            if not self.scripted_path:
                raise ValueError("kind=scripted requires scripted_path to be set")
        elif self.kind == "openai_compat":
            if not self.base_url:
                raise ValueError("kind=openai_compat requires base_url to be set")
            if not self.model:
                raise ValueError("kind=openai_compat requires model to be set")
        return self


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

    planner: int = 16384
    """The full plan JSON. Sized generously because plan generation is the
    single most critical call: a truncated plan fails the whole task. Local
    serving stacks (e.g. rapid-mlx) typically allow far larger generations,
    and structured-output prompts include the Qwen-3 ``/no_think`` soft switch
    so this budget is spent on the JSON itself, not on internal reasoning."""
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


class WebToolConfig(BaseModel):
    """The web integration: page fetch + search, both opt-in.

    These tools need the ``web`` extra (``pip install dagagent[web]``); enabling
    one without it raises a clear error at registration. Off by default — the
    base install stays lean and makes no outbound requests.
    """

    fetch: bool = False
    """Register ``web_fetch(url)`` — GET a page and extract its main text."""
    search: str = "none"
    """Search provider: ``none`` (disabled), ``duckduckgo``, or ``searxng``."""
    searxng_url: str = "http://localhost:8888"
    """Base URL of a self-hosted SearXNG instance (when ``search='searxng'``)."""
    timeout_s: float = 20.0
    """Per-request HTTP timeout for fetch and search."""
    max_results: int = 5
    """Number of search results to return."""


class ImageToolConfig(BaseModel):
    """Configuration for the image generation tool (mflux / Flux.1 Schnell).

    Opt-in: set ``enabled = true`` under ``[tools.image]`` in ``config.toml``.
    The mflux binary is auto-discovered at ``~/.venvs/mflux/bin/mflux-generate``
    or resolved from PATH; override with ``mflux_bin``.
    """

    enabled: bool = False
    mflux_bin: str = ""
    """Absolute path to the mflux-generate binary. Empty = auto-discover."""
    model: str = "schnell"
    """Flux model variant: ``schnell`` (fast, 4-step) or ``dev`` (quality)."""
    quantize: int = 4
    """Quantization bits for the mmdit transformer (4 or 8)."""
    default_steps: int = 4
    """Default diffusion steps. 4 is the right value for schnell."""
    default_width: int = 1024
    default_height: int = 1024
    low_ram: bool = True
    """Pass ``--low-ram`` to mflux to reduce peak memory pressure."""
    mlx_cache_limit_gb: int = 8
    """Cap the MLX cache (``--mlx-cache-limit-gb``) to bound memory usage."""
    timeout_s: float = 300.0
    """Per-generation timeout in seconds. First run may download weights."""


class ToolsSettings(BaseModel):
    """First-party tool integrations, each bundled and toggled by config.

    Distinct from third-party ``dagagent.tools`` entry-point plugins (gated by
    ``load_tool_plugins``): these ship in-tree and carry their own config.
    """

    web: WebToolConfig = WebToolConfig()
    image: ImageToolConfig = ImageToolConfig()


class ToolApprovalPolicy(StrEnum):
    """Per-tool approval behavior."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolApprovalMode(StrEnum):
    """How to interpret approval policies for this execution surface."""

    INTERACTIVE = "interactive"
    AUTO = "auto"


class ToolApprovalSettings(BaseModel):
    """Operator policy for tool execution approval."""

    default: ToolApprovalPolicy = ToolApprovalPolicy.ALLOW
    """Policy for tools without an override."""
    mode: ToolApprovalMode = ToolApprovalMode.INTERACTIVE
    """In auto mode, ask is interpreted as allow; deny still denies."""
    overrides: dict[str, ToolApprovalPolicy] = Field(default_factory=dict)
    """Per-tool policy overrides, keyed by tool name."""

    def policy_for(self, tool_name: str) -> ToolApprovalPolicy:
        return self.overrides.get(tool_name, self.default)


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
    # Sized for real fan-out: a "for each of N things, do M things" research
    # task needs N*M tool nodes plus aggregation (a 3x3 sweep is already 14).
    # The planner is told this budget (it's injected into the prompt), so the
    # cap is a backstop against runaway plans, not the model's working limit.
    max_plan_nodes: int = 24
    max_decision_nesting_depth: int = 2
    max_replan_attempts: int = 1
    planner_stall_char_multiplier: int = 4
    """Classify invalid planner output longer than planner max_tokens times
    this multiplier as a stall and feed the raw output into the retry prompt."""
    planner_min_tier: int = 0
    """Minimum tier the planner is allowed to use. Set to 1 when a faster/cheaper
    model occupies tier 0 so that planning always uses the quality tier."""
    planner_initial_timeout_s: int = 150
    """Ceiling on the first (no-think) planning attempt, in seconds.
    Only effective when the tier's ``timeout_s`` is longer than this value.
    For local MLX models set ``DAGAGENT_TIER0__TIMEOUT_S`` to at least 180.
    Empirically: qwen3.6-35b-moe on Apple Silicon needs 60-150s to prefill
    and emit plan JSON for complex prompts (decision, research, subplan tasks)
    without extended reasoning; 150s covers the observed range with headroom."""
    planner_retry_timeout_s: int = 240
    """Ceiling on the retry attempt (thinking re-enabled) in seconds.
    Same tier-timeout interaction applies — the tier timeout must be >= this
    value for the ceiling to be effective."""
    confidence_threshold: float = 0.75
    max_subplan_nodes: int = 12
    max_subplans_per_plan: int = 2

    # ── Generation ceiling ───────────────────────────────────────────────
    # A hard upper bound on max_tokens sent to any tier on a single request,
    # independent of the per-node budget. The budget is the goal; this is the
    # safety backstop that caps worst-case memory consumption per generation
    # across long multi-step runs. Any per-node budget above this ceiling is
    # clamped to it before the request leaves the router.
    # Default matches the MLX server's --max-tokens 20000 so no budget is
    # silently truncated out of the box; lower it to tighten the backstop.
    max_generation_tokens: int = 20000

    # ── Per-node output-token budgets ────────────────────────────────────
    budgets: NodeBudgets = NodeBudgets()

    # ── Per-node system prompts ──────────────────────────────────────────
    prompts: NodePrompts = NodePrompts()

    # ── Tools ────────────────────────────────────────────────────────────
    # Built-in tools always register. First-party integrations (web, later
    # obsidian/calendar) are bundled and toggled under ``tools``. Third-party
    # tools advertised through the ``dagagent.tools`` entry-point group are a
    # separate opt-in: discovering whatever is installed is a deliberate choice.
    tools: ToolsSettings = ToolsSettings()
    load_tool_plugins: bool = False

    # ── HITL ─────────────────────────────────────────────────────────────
    confirm_plans: bool = False
    tool_approval: ToolApprovalSettings = ToolApprovalSettings()

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
        # Two TOML sources in descending priority order:
        # 1. config.toml in CWD — used during development / eval runs from the
        #    project root, and lets per-project settings override the home config.
        # 2. ~/.dagagent/config.toml — the persistent user config. Loaded
        #    regardless of CWD so the TUI (which spawns dagagent acp from an
        #    arbitrary directory) always picks up the user's settings.
        #    Override the home directory via DAGAGENT_CONFIG_HOME (used in tests
        #    to point at a non-existent path so the home config is not loaded).
        import os  # noqa: PLC0415

        _cfg_home = Path(os.environ.get("DAGAGENT_CONFIG_HOME", Path.home() / ".dagagent"))
        _home_cfg = _cfg_home / "config.toml"
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file="config.toml"),
            TomlConfigSettingsSource(settings_cls, toml_file=_home_cfg),
            file_secret_settings,
        )
