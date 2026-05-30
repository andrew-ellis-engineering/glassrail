"""Unit tests for the TUI view model.

Pure state-folding + rendering, no server. Renders through a non-terminal
console to confirm the renderable never raises at any stage.
"""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console

from dagagent.gateways.tui import TaskView


def _happy_path() -> list[dict[str, Any]]:
    return [
        {"type": "planning_started", "task_id": "t"},
        {"type": "plan_ready", "task_id": "t", "node_count": 2},
        {"type": "node_started", "task_id": "t", "node_id": 1, "node_type": "tool", "tier": 0},
        {
            "type": "node_finished",
            "task_id": "t",
            "node_id": 1,
            "status": "completed",
            "confidence": 1.0,
            "flagged": False,
            "tier_used": 0,
        },
        {"type": "node_started", "task_id": "t", "node_id": 2, "node_type": "result", "tier": 0},
        {
            "type": "node_finished",
            "task_id": "t",
            "node_id": 2,
            "status": "completed",
            "confidence": 0.9,
            "flagged": False,
        },
        {"type": "task_completed", "task_id": "t", "final_output": "nothing scheduled."},
    ]


def test_view_folds_events_into_state() -> None:
    view = TaskView(request="what's today?")
    for event in _happy_path():
        view.ingest(event)

    assert view.status == "completed"
    assert view.node_count == 2
    assert view.done is True
    assert view.final_output == "nothing scheduled."
    assert set(view.nodes) == {1, 2}
    assert view.nodes[1].node_type == "tool"
    assert view.nodes[2].status == "completed"


def test_view_renders_at_every_stage() -> None:
    buf = io.StringIO()
    console = Console(file=buf, width=90)
    view = TaskView(request="what's today?")
    for event in _happy_path():
        view.ingest(event)
        console.print(view.render())  # must never raise
    assert "dagagent" in buf.getvalue()


def test_view_renders_dag_when_plan_present() -> None:
    buf = io.StringIO()
    console = Console(file=buf, width=90)
    view = TaskView(request="research the thing")
    view.ingest(
        {
            "type": "plan_ready",
            "task_id": "t",
            "node_count": 2,
            "plan": {
                "nodes": [
                    {"id": 1, "type": "tool", "tool": "web_search", "context_needed": []},
                    {"id": 2, "type": "result", "context_needed": [1]},
                ]
            },
        }
    )
    view.ingest(
        {"type": "node_started", "task_id": "t", "node_id": 1, "node_type": "tool", "tier": 0}
    )
    console.print(view.render())  # must never raise
    out = buf.getvalue()
    assert "plan" in out  # the DAG panel title
    assert "web_search" in out  # a node box was drawn


def test_no_dag_omits_the_dag_panel() -> None:
    buf = io.StringIO()
    console = Console(file=buf, width=90)
    view = TaskView(request="x", show_dag=False)
    view.ingest(
        {
            "type": "plan_ready",
            "task_id": "t",
            "node_count": 1,
            "plan": {"nodes": [{"id": 1, "type": "result", "context_needed": []}]},
        }
    )
    console.print(view.render())
    assert "plan" not in buf.getvalue()  # no DAG panel


def test_view_records_branch_and_failure() -> None:
    view = TaskView(request="x")
    view.ingest(
        {"type": "node_started", "task_id": "t", "node_id": 2, "node_type": "decision", "tier": 0}
    )
    view.ingest(
        {
            "type": "branch_decided",
            "task_id": "t",
            "node_id": 2,
            "branch_taken": "no",
            "confidence": 0.8,
        }
    )
    assert view.nodes[2].branch_taken == "no"

    view.ingest({"type": "task_failed", "task_id": "t", "error": "boom"})
    assert view.status == "failed"
    assert view.error == "boom"
    assert view.done is True


def test_view_records_failed_planning_attempts() -> None:
    view = TaskView(request="x")
    view.ingest(
        {
            "type": "plan_failed",
            "task_id": "t",
            "error": "planning failed",
            "attempts": [
                {
                    "attempt": 0,
                    "raw_output": "not json",
                    "error": "Planner returned invalid JSON",
                }
            ],
        }
    )
    assert view.status == "failed"
    assert view.planning_attempts[0]["raw_output"] == "not json"


def test_view_ignores_unknown_event() -> None:
    view = TaskView(request="x")
    view.ingest({"type": "mystery"})
    assert view.status == "planning"
    assert view.done is False
