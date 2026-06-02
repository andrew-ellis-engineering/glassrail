"""Translate dag-agent's plan and node vocabulary into ACP shapes.

ACP's ``plan`` update carries the *entire* entry list every time and the
client replaces its view, so we keep a small per-turn tracker that remembers
each node's status and re-renders the full entry list on every change.

ACP's plan-status enum is coarser than :class:`NodeStatus` — only
``pending | in_progress | completed``. The mapping is kept here, in one place,
so the lossy edges (``skipped``, ``failed``) are handled consistently:
``skipped`` collapses to ``completed`` and is annotated in the entry text;
``failed`` has no plan equivalent and is surfaced by the server as a message
plus the turn's stop reason.
"""

from __future__ import annotations

from typing import Any

from dagagent.core import NodeStatus

# ACP plan entry status values.
ACP_PENDING = "pending"
ACP_IN_PROGRESS = "in_progress"
ACP_COMPLETED = "completed"


def acp_plan_status(status: NodeStatus | None, *, running: bool = False) -> str:
    """Map a dag-agent node status to ACP's three-value plan status."""
    if running and status in (None, NodeStatus.PENDING):
        return ACP_IN_PROGRESS
    if status in (NodeStatus.COMPLETED, NodeStatus.SKIPPED, NodeStatus.EMPTY, NodeStatus.FAILED):
        return ACP_COMPLETED
    return ACP_PENDING


class PlanTracker:
    """Holds a turn's node descriptions and statuses; renders ACP plan entries."""

    def __init__(self) -> None:
        self._desc: dict[int, str] = {}
        self._type: dict[int, str] = {}
        self._tool: dict[int, str | None] = {}
        self._args: dict[int, dict[str, Any]] = {}
        self._order: list[int] = []
        self._status: dict[int, NodeStatus] = {}
        self._running: set[int] = set()

    def load(self, plan: dict[str, Any]) -> None:
        """Seed the tracker from a serialised :class:`Plan` (all nodes pending)."""
        self._desc.clear()
        self._type.clear()
        self._tool.clear()
        self._args.clear()
        self._order.clear()
        self._status.clear()
        self._running.clear()
        nodes: list[dict[str, Any]] = plan.get("nodes") or []
        order: list[int] = plan.get("sorted_node_ids") or [n["id"] for n in nodes]
        by_id: dict[int, dict[str, Any]] = {n["id"]: n for n in nodes}
        for node_id in order:
            node = by_id.get(node_id)
            if node is None:
                continue
            self._order.append(node_id)
            kind: str = node.get("type", "node")
            self._type[node_id] = kind
            self._tool[node_id] = node.get("tool")
            self._args[node_id] = node.get("args_template") or {}
            self._desc[node_id] = f"[{kind}] {node.get('description', '')}".strip()

    def node_type(self, node_id: int) -> str | None:
        return self._type.get(node_id)

    def tool_name(self, node_id: int) -> str | None:
        return self._tool.get(node_id)

    def tool_input(self, node_id: int) -> dict[str, Any]:
        return self._args.get(node_id, {})

    def description(self, node_id: int) -> str:
        return self._desc.get(node_id, "")

    def start(self, node_id: int) -> None:
        self._running.add(node_id)

    def finish(self, node_id: int, status: NodeStatus) -> None:
        self._running.discard(node_id)
        self._status[node_id] = status

    def entries(self) -> list[dict[str, Any]]:
        """Render the full ACP plan-entry list in topological order."""
        out: list[dict[str, Any]] = []
        for node_id in self._order:
            status = self._status.get(node_id)
            content = self._desc.get(node_id, "")
            if status is NodeStatus.SKIPPED:
                content = f"{content} (skipped)"
            elif status is NodeStatus.FAILED:
                content = f"{content} (failed)"
            out.append(
                {
                    "nodeId": node_id,
                    "content": content,
                    "priority": "medium",
                    "status": acp_plan_status(status, running=node_id in self._running),
                }
            )
        return out
