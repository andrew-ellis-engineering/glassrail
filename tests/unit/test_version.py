"""Smoke test that the package imports and exposes a version."""

from __future__ import annotations

import dagagent


def test_version_is_string() -> None:
    assert isinstance(dagagent.__version__, str)
    assert dagagent.__version__ != ""
