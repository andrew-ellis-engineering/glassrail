"""Tests for filesystem path confinement."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from glassrail.core import ToolExecutionError
from glassrail.harness.pathguard import ensure_within_roots, reset_unconfined_warning_for_tests


def test_pathguard_allows_path_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "note.txt"
    target.write_text("ok")

    assert ensure_within_roots(str(target), [root]) == target.resolve()


def test_pathguard_denies_traversal_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    with pytest.raises(ToolExecutionError, match=r"outside the configured tools\.fs_roots"):
        ensure_within_roots(str(root / ".." / "outside.txt"), [root])


def test_pathguard_denies_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    escape = root / "escape.txt"
    escape.symlink_to(outside)

    with pytest.raises(ToolExecutionError, match=r"outside the configured tools\.fs_roots"):
        ensure_within_roots(str(escape), [root])


def test_pathguard_unconfined_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    reset_unconfined_warning_for_tests()
    caplog.set_level(logging.WARNING, logger="glassrail.harness.pathguard")

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    assert ensure_within_roots(str(first), None) == first.resolve()
    assert ensure_within_roots(str(second), []) == second.resolve()

    warnings = [
        record.message for record in caplog.records if "file tools are unconfined" in record.message
    ]
    assert warnings == ["file tools are unconfined; set tools.fs_roots to restrict them"]
