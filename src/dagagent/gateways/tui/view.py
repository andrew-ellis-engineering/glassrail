"""View model for the TUI — fold the event stream into a renderable.

Pure and transport-free: :meth:`TaskView.ingest` updates state from a decoded
event dict, and :meth:`TaskView.render` turns the current state into a Rich
renderable. Keeping the rendering a pure function of accumulated state makes it
testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeGuard

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_STATUS_STYLE = {
    "planning": "yellow",
    "executing": "cyan",
    "awaiting_confirmation": "magenta",
    "completed": "green",
    "failed": "red",
}

_NODE_STATUS_STYLE = {
    "running": "yellow",
    "completed": "green",
    "skipped": "dim",
    "failed": "red",
    "empty": "dim yellow",
}


@dataclass
class _NodeRow:
    node_id: int
    node_type: str
    status: str = "running"
    tier: int | None = None
    confidence: float | None = None
    flagged: bool = False
    branch_taken: str | None = None


@dataclass
class TaskView:
    """Accumulates task events and renders them as a live progress view."""

    request: str
    status: str = "planning"
    node_count: int | None = None
    nodes: dict[int, _NodeRow] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    planning_attempts: list[dict[str, Any]] = field(default_factory=list)
    final_output: str | None = None
    error: str | None = None
    done: bool = False

    def ingest(self, event: dict[str, Any]) -> None:
        """Update state from one decoded event dict (unknown types ignored)."""
        etype = str(event.get("type", ""))
        if etype == "planning_started":
            self.status = "planning"
        elif etype == "plan_ready":
            self.status = "executing"
            self.node_count = _as_int(event.get("node_count"))
            plan = event.get("plan")
            self.plan = plan if isinstance(plan, dict) else None
        elif etype == "node_started":
            row = self._row(event)
            row.node_type = str(event.get("node_type", row.node_type))
            row.tier = _as_int(event.get("tier"))
            row.status = "running"
        elif etype == "node_finished":
            row = self._row(event)
            row.status = str(event.get("status", row.status))
            row.confidence = _as_float(event.get("confidence"))
            row.flagged = bool(event.get("flagged", False))
            row.tier = _as_int(event.get("tier_used")) or row.tier
            error = event.get("error")
            if isinstance(error, str) and error:
                row.status = f"{row.status}: {error}"
        elif etype == "branch_decided":
            row = self._row(event)
            branch = event.get("branch_taken")
            row.branch_taken = str(branch) if branch is not None else None
        elif etype == "task_completed":
            self.status = "completed"
            self.final_output = _as_str(event.get("final_output"))
            self.done = True
        elif etype in ("task_failed", "plan_failed"):
            self.status = "failed"
            self.error = _as_str(event.get("error"))
            attempts = event.get("attempts")
            self.planning_attempts = attempts if _is_attempt_list(attempts) else []
            self.done = True
        elif etype == "awaiting_confirmation":
            self.status = "awaiting_confirmation"
            self.node_count = _as_int(event.get("node_count"))
            self.done = True

    def _row(self, event: dict[str, Any]) -> _NodeRow:
        node_id = _as_int(event.get("node_id")) or 0
        row = self.nodes.get(node_id)
        if row is None:
            row = _NodeRow(node_id=node_id, node_type=str(event.get("node_type", "")))
            self.nodes[node_id] = row
        return row

    def render(self) -> RenderableType:
        """Build the current Rich view: a header, the node table, a footer."""
        style = _STATUS_STYLE.get(self.status, "white")
        header = Text.assemble(
            ("task ", "bold"),
            (self.request, "italic"),
            ("\nstatus: ", "bold"),
            (self.status, style),
            *(((f"   nodes: {self.node_count}", "dim"),) if self.node_count is not None else ()),
        )

        table = Table(expand=True, show_edge=False, pad_edge=False)
        table.add_column("node", justify="right", no_wrap=True)
        table.add_column("type")
        table.add_column("tier", justify="right")
        table.add_column("status")
        table.add_column("conf", justify="right")
        for row in self.nodes.values():
            node_style = _NODE_STATUS_STYLE.get(row.status, "white")
            status_text = row.status + (" ⚑" if row.flagged else "")
            if row.branch_taken is not None:
                status_text = f"{status_text} → {row.branch_taken}"
            table.add_row(
                str(row.node_id),
                row.node_type,
                "" if row.tier is None else str(row.tier),
                Text(status_text, style=node_style),
                "" if row.confidence is None else f"{row.confidence:.2f}",
            )

        body: list[RenderableType] = [header, table]
        if self.final_output is not None:
            body.append(Panel(self.final_output, title="result", border_style="green"))
        if self.error is not None:
            body.append(Panel(self.error, title="error", border_style="red"))
        if self.planning_attempts:
            body.append(
                Panel(
                    _format_attempts(self.planning_attempts),
                    title="planning attempts",
                    border_style="red",
                )
            )

        return Panel(Group(*body), title="dagagent", border_style=style)


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _is_attempt_list(value: Any) -> TypeGuard[list[dict[str, Any]]]:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _format_attempts(attempts: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for attempt in attempts:
        number = attempt.get("attempt", "?")
        error = attempt.get("error") or "valid"
        raw = str(attempt.get("raw_output", ""))
        snippet = raw.replace("\n", " ")[:240]
        lines.append(f"{number}: {error}")
        if snippet:
            lines.append(f"raw: {snippet}")
    return "\n".join(lines)
