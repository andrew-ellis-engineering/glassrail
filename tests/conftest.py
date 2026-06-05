"""Project-wide pytest fixtures.

Subpackage-specific fixtures live in their own conftest.py. Anything needed
by more than one subtree (event bus, ULID seeding, etc.) belongs here.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate tests from the user's persistent config files.

    config.toml (CWD) sets planner_min_tier=1 and points tiers at local
    servers; ~/.dagagent/config.toml does the same. Tests use scripted
    providers at tier 0, so we reset both settings here rather than have
    every Settings() construction deal with production overrides.
    """
    monkeypatch.setenv("DAGAGENT_PLANNER_MIN_TIER", "0")
    # Point the home config directory at a non-existent path so tests
    # don't pick up ~/.dagagent/config.toml.
    monkeypatch.setenv("DAGAGENT_CONFIG_HOME", "/nonexistent/dagagent-test")
