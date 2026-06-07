"""Smoke test that the package imports and exposes a version."""

from __future__ import annotations

import glassrail


def test_version_is_string() -> None:
    assert isinstance(glassrail.__version__, str)
    assert glassrail.__version__ != ""
