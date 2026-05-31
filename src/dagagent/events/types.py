"""Typed events emitted as a task is planned and executed.

Every event carries a ``task_id`` and a UTC ``timestamp``, plus a string
``type`` discriminator so a serialised stream (SSE, WebSocket) is
self-describing. Producers construct concrete subclasses; consumers receive
them through :class:`dagagent.events.bus.EventBus`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from dagagent.core import NodeStatus, NodeType, TaskId


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _BaseEvent(BaseModel):
    """Fields shared by every event."""

    task_id: TaskId
    timestamp: datetime = Field(default_factory=_utcnow)


class PlanningStarted(_BaseEvent):
    type: Literal["planning_started"] = "planning_started"


class PlanReady(_BaseEvent):
    type: Literal["plan_ready"] = "plan_ready"
    node_count: int
    plan: dict[str, Any] | None = None


class PlanFailed(_BaseEvent):
    type: Literal["plan_failed"] = "plan_failed"
    error: str
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class AwaitingConfirmation(_BaseEvent):
    type: Literal["awaiting_confirmation"] = "awaiting_confirmation"
    node_count: int


class NodeStarted(_BaseEvent):
    type: Literal["node_started"] = "node_started"
    node_id: int
    node_type: NodeType
    tier: int


class NodeFinished(_BaseEvent):
    type: Literal["node_finished"] = "node_finished"
    node_id: int
    status: NodeStatus
    confidence: float
    flagged: bool
    tier_used: int | None = None
    error: str | None = None


class BranchDecided(_BaseEvent):
    type: Literal["branch_decided"] = "branch_decided"
    node_id: int
    branch_taken: str | None
    confidence: float


class TaskCompleted(_BaseEvent):
    type: Literal["task_completed"] = "task_completed"
    final_output: str | None = None


class TaskFailed(_BaseEvent):
    type: Literal["task_failed"] = "task_failed"
    error: str
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class TaskCancelled(_BaseEvent):
    type: Literal["task_cancelled"] = "task_cancelled"


Event = (
    PlanningStarted
    | PlanReady
    | PlanFailed
    | AwaitingConfirmation
    | NodeStarted
    | NodeFinished
    | BranchDecided
    | TaskCompleted
    | TaskFailed
    | TaskCancelled
)
"""Union of every event type — the value the bus carries."""

# Events that mark the end of a task's lifecycle. A subscriber streaming one
# task's events can stop once it sees one of these.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"task_completed", "task_failed", "plan_failed", "awaiting_confirmation", "task_cancelled"}
)
