"""Smoke test that the package imports and exposes a version."""

from __future__ import annotations

from importlib.metadata import version

import glassrail


def test_version_is_string() -> None:
    assert isinstance(glassrail.__version__, str)
    assert glassrail.__version__ != ""


def test_version_matches_package_metadata() -> None:
    assert glassrail.__version__ == version("glassrail")
