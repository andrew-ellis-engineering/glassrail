"""Unit tests for the TUI DAG renderer.

Pure: the layering is a function of the plan structure, and rendering is a
function of plan + per-node status. Plain-text assertions go through a
non-terminal console (no ANSI), so they read the box-drawing glyphs and labels
directly. A wide console exercises the grid (with routed edges); a narrow one
exercises the vertical-list fallback.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from glassrail.gateways.tui.dag import plan_layers, render_dag

_BOX = "┌"  # a box corner — present only in the grid view, not the fallback


@dataclass
class _Row:
    """Minimal stand-in for the view's ``_NodeRow`` (satisfies ``RowLike``)."""

    status: str
    branch_taken: str | None = None
    flagged: bool = False


def _plan() -> dict[str, Any]:
    """A fan-out → fan-in plan gated by a decision.

    layers: [1,2] → [3,4] → [5] → [6,7]
    """
    return {
        "nodes": [
            {"id": 1, "type": "tool", "tool": "web_search", "description": "search A"},
            {"id": 2, "type": "tool", "tool": "web_search", "description": "search B"},
            {"id": 3, "type": "summary", "description": "condense A", "context_needed": [1]},
            {"id": 4, "type": "summary", "description": "condense B", "context_needed": [2]},
            {
                "id": 5,
                "type": "decision",
                "condition": "enough?",
                "context_needed": [3, 4],
                "branches": {"yes": [6], "no": [7]},
            },
            {"id": 6, "type": "synthesis", "description": "merge", "context_needed": []},
            {"id": 7, "type": "result", "description": "answer", "context_needed": []},
        ]
    }


def _plain(renderable: Any, width: int = 120) -> str:
    buf = io.StringIO()
    Console(file=buf, width=width).print(renderable)
    return buf.getvalue()


def test_layers_rank_by_longest_path() -> None:
    assert plan_layers(_plan()) == [[1, 2], [3, 4], [5], [6, 7]]


def test_branch_targets_sink_below_their_decision() -> None:
    """A branch target with no context_needed still lands under its decision."""
    plan: dict[str, Any] = {
        "nodes": [
            {"id": 1, "type": "decision", "branches": {"yes": [2], "no": [3]}},
            {"id": 2, "type": "synthesis", "context_needed": []},
            {"id": 3, "type": "result", "context_needed": []},
        ]
    }
    assert plan_layers(plan) == [[1], [2, 3]]


def test_grid_draws_boxes_with_titles_and_summaries() -> None:
    out = _plain(render_dag(_plan(), {}))
    assert _BOX in out  # boxes are drawn
    assert "web_search" in out  # tool name in the box title
    assert "search A" in out  # node 1's description, surfaced as its summary
    assert "condense A" in out  # node 3's description


def test_grid_draws_routed_edges() -> None:
    out = _plain(render_dag(_plan(), {}))
    # A box teed out the bottom, a box teed into the top, and vertical runs.
    assert "┬" in out
    assert "┴" in out
    assert "│" in out


def test_long_description_is_truncated() -> None:
    plan: dict[str, Any] = {
        "nodes": [{"id": 1, "type": "synthesis", "description": "x" * 200, "context_needed": []}]
    }
    out = _plain(render_dag(plan, {}))
    assert "…" in out
    assert "x" * 200 not in out


def test_decision_box_shows_condition_and_taken_branch() -> None:
    out = _plain(render_dag(_plan(), {5: _Row(status="completed", branch_taken="yes")}))
    assert "enough?" in out
    assert "→yes" in out


def test_status_glyphs_track_each_nodes_state() -> None:
    rows = {
        1: _Row(status="completed"),
        2: _Row(status="running"),
        3: _Row(status="failed: boom"),  # collapses on the colon
    }
    out = _plain(render_dag(_plan(), rows))
    assert "●" in out  # completed
    assert "◐" in out  # running
    assert "✗" in out  # failed
    assert "○" in out  # nodes 4-7 are pending


def test_flag_is_marked() -> None:
    out = _plain(render_dag(_plan(), {6: _Row(status="completed", flagged=True)}))
    assert "⚑" in out


def test_subplan_node_shows_size() -> None:
    plan: dict[str, Any] = {
        "nodes": [
            {"id": 1, "type": "subplan", "subplan": {"nodes": [{"id": 1}, {"id": 2}, {"id": 3}]}},
        ]
    }
    assert "(3 nodes)" in _plain(render_dag(plan, {}))


def test_narrow_terminal_falls_back_to_a_list() -> None:
    out = _plain(render_dag(_plan(), {}), width=40)
    assert _BOX not in out  # no grid boxes at this width
    assert "layer 0" in out  # the list groups by layer
    assert "← 3,4" in out  # dependencies are spelled out instead of drawn


def test_empty_or_malformed_plan_renders_nothing() -> None:
    assert _plain(render_dag({}, {})).strip() == ""
    assert _plain(render_dag({"nodes": "not a list"}, {})).strip() == ""
    # A node list with no valid (int-id, dict) entries yields no layers.
    assert plan_layers({"nodes": [{"no_id": True}, "garbage"]}) == []
