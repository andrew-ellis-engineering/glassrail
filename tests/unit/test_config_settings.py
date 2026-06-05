"""Tests for the Settings loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from dagagent.config import Settings, TierConfig, ToolApprovalMode, ToolApprovalPolicy


def _clear_env(monkeypatch: MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("DAGAGENT_"):
            monkeypatch.delenv(key, raising=False)
    # Keep the home config dir pointed at a non-existent path so tests
    # that exercise pure defaults are not affected by ~/.dagagent/config.toml.
    monkeypatch.setenv("DAGAGENT_CONFIG_HOME", "/nonexistent/dagagent-test")


def test_defaults(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_env(monkeypatch)
    settings = Settings()
    assert settings.tier0.model == "qwen3.6-35b-moe"
    assert settings.tier0.timeout_s == 10.0
    assert settings.tier1.model == "deepseek/deepseek-v4-flash"
    assert settings.tier3.model == "anthropic/claude-sonnet-4-6"
    assert settings.max_plan_nodes == 24
    assert settings.confidence_threshold == 0.75
    assert settings.confirm_plans is False
    assert settings.tool_approval.default is ToolApprovalPolicy.ALLOW
    assert settings.tool_approval.mode is ToolApprovalMode.INTERACTIVE
    assert settings.tool_approval.overrides == {}
    assert settings.state_path == Path("./state.sqlite")
    assert len(settings.tiers) == 4


def test_env_var_overrides_top_level(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("DAGAGENT_MAX_PLAN_NODES", "20")
    monkeypatch.setenv("DAGAGENT_CONFIRM_PLANS", "true")
    settings = Settings()
    assert settings.max_plan_nodes == 20
    assert settings.confirm_plans is True


def test_env_var_overrides_nested_tier(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("DAGAGENT_TIER1__MODEL", "openai/gpt-9000")
    monkeypatch.setenv("DAGAGENT_TIER1__API_KEY", "secret-123")
    settings = Settings()
    assert settings.tier1.model == "openai/gpt-9000"
    assert settings.tier1.api_key == "secret-123"
    # Untouched defaults remain.
    assert settings.tier1.base_url == "https://openrouter.ai/api/v1"


def test_tool_approval_settings_parse(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("DAGAGENT_TOOL_APPROVAL__MODE", "auto")
    monkeypatch.setenv("DAGAGENT_TOOL_APPROVAL__OVERRIDES__file_write", "ask")
    monkeypatch.setenv("DAGAGENT_TOOL_APPROVAL__OVERRIDES__shell_exec", "deny")

    settings = Settings()

    assert settings.tool_approval.mode is ToolApprovalMode.AUTO
    assert settings.tool_approval.policy_for("file_write") is ToolApprovalPolicy.ASK
    assert settings.tool_approval.policy_for("shell_exec") is ToolApprovalPolicy.DENY
    assert settings.tool_approval.policy_for("file_read") is ToolApprovalPolicy.ALLOW


def test_init_kwargs_win_over_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("DAGAGENT_MAX_PLAN_NODES", "20")
    settings = Settings(max_plan_nodes=99)
    assert settings.max_plan_nodes == 99


def test_toml_file_overrides_defaults(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        """
        max_plan_nodes = 7
        confidence_threshold = 0.5

        [tier1]
        model = "anthropic/claude-haiku-4-5"
        """
    )
    settings = Settings()
    assert settings.max_plan_nodes == 7
    assert settings.confidence_threshold == 0.5
    assert settings.tier1.model == "anthropic/claude-haiku-4-5"


def test_env_beats_toml(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("max_plan_nodes = 7\n")
    monkeypatch.setenv("DAGAGENT_MAX_PLAN_NODES", "99")
    settings = Settings()
    assert settings.max_plan_nodes == 99


def test_tier_config_validates() -> None:
    tier = TierConfig(base_url="http://x", model="m")
    assert tier.api_key == ""
    assert tier.timeout_s == 60.0


def test_tiers_property_is_ordered() -> None:
    settings = Settings(
        tier0=TierConfig(base_url="a", model="m0"),
        tier1=TierConfig(base_url="b", model="m1"),
        tier2=TierConfig(base_url="c", model="m2"),
        tier3=TierConfig(base_url="d", model="m3"),
    )
    assert [t.model for t in settings.tiers] == ["m0", "m1", "m2", "m3"]


def test_extra_env_vars_ignored(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("DAGAGENT_UNKNOWN_FIELD", "whatever")
    # Should not raise.
    settings = Settings()
    assert not hasattr(settings, "unknown_field")


@pytest.mark.parametrize(
    "bool_str,expected",
    [("true", True), ("false", False), ("1", True), ("0", False)],
)
def test_bool_parsing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    bool_str: str,
    expected: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_env(monkeypatch)
    monkeypatch.setenv("DAGAGENT_CONFIRM_PLANS", bool_str)
    settings = Settings()
    assert settings.confirm_plans is expected
