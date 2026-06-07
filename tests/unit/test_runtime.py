"""Tests for the runtime composition root."""

from __future__ import annotations

import pytest

from glassrail.config import Settings
from glassrail.harness import ToolHarness
from glassrail.runtime import build_runtime


def _spy_load_entry_points(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ToolHarness.load_entry_points to record the groups it's asked for."""
    calls: list[str] = []

    def fake_load(self: ToolHarness, group: str = "glassrail.tools") -> int:
        calls.append(group)
        return 0

    monkeypatch.setattr(ToolHarness, "load_entry_points", fake_load)
    return calls


def test_build_runtime_skips_tool_plugins_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_load_entry_points(monkeypatch)
    build_runtime(Settings())
    assert calls == []


def test_build_runtime_loads_tool_plugins_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_load_entry_points(monkeypatch)
    build_runtime(Settings(load_tool_plugins=True))
    assert calls == ["glassrail.tools"]
