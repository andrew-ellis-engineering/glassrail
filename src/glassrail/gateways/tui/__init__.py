"""Terminal client — submit a task to a running gateway and watch it run.

A thin client (:func:`stream_task_events`) feeds a pure view model
(:class:`TaskView`) that a ``rich.Live`` loop (:func:`run_tui`) renders as
plan → per-node progress → final output. Wired to the CLI as ``glassrail tui``.
"""

from __future__ import annotations

from glassrail.gateways.tui.app import run_tui
from glassrail.gateways.tui.client import DEFAULT_BASE_URL, stream_task_events
from glassrail.gateways.tui.view import TaskView

__all__ = ["DEFAULT_BASE_URL", "TaskView", "run_tui", "stream_task_events"]
