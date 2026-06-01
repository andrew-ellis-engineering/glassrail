"""Execution state — what the system records as a plan runs.

The :class:`ExecutionState` is the durable record of a task: the plan, every
node result, the branch decision log, and lifecycle status. It is fully
serialisable so a process restart can resume work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from dagagent.core.ids import TaskId
from dagagent.core.plan import Plan


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NodeStatus(StrEnum):
    """Per-node lifecycle."""

    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    """Excluded by a branch decision upstream."""
    FAILED = "failed"
    EMPTY = "empty"
    """Tool returned nothing — not an error, but worth flagging."""


class NodeResult(BaseModel):
    """Outcome of one node's execution."""

    node_id: int
    status: NodeStatus
    output: Any = None
    branch_taken: str | None = None
    """Set on DECISION nodes — names the branch the executor took."""
    confidence: float = 1.0
    flagged: bool = False
    """True when ``confidence`` fell below the configured threshold."""
    tokens_used: int = 0
    execution_time_s: float = 0.0
    error: str | None = None
    tier_used: int | None = None


class BranchLogEntry(BaseModel):
    """A single decision-node outcome, recorded for audit."""

    node_id: int
    condition: str | None
    branch_taken: str | None
    confidence: float
    timestamp: datetime


class PlanningAttempt(BaseModel):
    """One planner attempt, retained for debugging failed or flaky plans."""

    attempt: int
    raw_output: str
    parsed: dict[str, Any] | None = None
    plan: Plan | None = None
    error: str | None = None
    error_type: str | None = None
    tokens_used: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    filepath: str | None = None

    @property
    def valid(self) -> bool:
        """Whether this attempt produced a validated plan."""
        return self.plan is not None and self.error is None


class TaskStatus(StrEnum):
    """Top-level lifecycle of a task."""

    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    """HITL gate — orchestrator paused, awaiting user resume."""
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    """Mid-execution pause for resume."""
    CANCELLED = "cancelled"
    """Interrupted by the client (e.g. ACP session/cancel)."""
    REJECTED = "rejected"
    """Planner determined the task cannot be completed with available tools."""


class ExecutionState(BaseModel):
    """Serialisable state for a single task."""

    task_id: TaskId
    user_request: str
    plan: Plan | None = None

    results: dict[int, NodeResult] = Field(default_factory=dict)
    completed_nodes: list[int] = Field(default_factory=list)
    skipped_nodes: list[int] = Field(default_factory=list)
    branch_log: list[BranchLogEntry] = Field(default_factory=list)
    planning_attempts: list[PlanningAttempt] = Field(default_factory=list)

    status: TaskStatus = TaskStatus.PLANNING
    replan_count: int = 0

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    final_output: str | None = None
    error: str | None = None

    def touch(self) -> None:
        """Stamp ``updated_at`` with the current UTC time."""
        self.updated_at = _utcnow()
