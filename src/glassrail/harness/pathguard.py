"""Filesystem path confinement for first-party tools."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from glassrail.core import ToolExecutionError

log = logging.getLogger(__name__)


@dataclass
class _WarningState:
    unconfined: bool = False


_warning_state = _WarningState()


def reset_unconfined_warning_for_tests() -> None:
    """Reset the one-time warning latch for isolated tests."""
    _warning_state.unconfined = False


def ensure_within_roots(path: str, roots: Sequence[Path] | None) -> Path:
    """Resolve ``path`` and ensure it stays under one configured root."""
    resolved = Path(path).expanduser().resolve()
    if not roots:
        if not _warning_state.unconfined:
            log.warning("file tools are unconfined; set tools.fs_roots to restrict them")
            _warning_state.unconfined = True
        return resolved

    resolved_roots = [root.expanduser().resolve() for root in roots]
    if any(resolved.is_relative_to(root) for root in resolved_roots):
        return resolved

    raise ToolExecutionError(f"path '{path}' is outside the configured tools.fs_roots")
