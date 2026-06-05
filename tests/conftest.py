"""Project-wide pytest fixtures.

Subpackage-specific fixtures live in their own conftest.py. Anything needed
by more than one subtree (event bus, ULID seeding, etc.) belongs here.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_planner_min_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset planner_min_tier to 0 for all tests.

    config.toml in the project root sets planner_min_tier=1 for production
    use (routes planner calls to the quality tier). Tests use scripted
    providers at tier 0, so this fixture ensures the local config.toml does
    not cause every test that instantiates Settings() to fail.
    """
    monkeypatch.setenv("DAGAGENT_PLANNER_MIN_TIER", "0")
