"""Core domain types shared by every other subpackage.

Importing from ``glassrail.core`` is the canonical way to refer to these types.
Nothing in ``glassrail.core`` may import from any other ``glassrail`` subpackage.
"""

from __future__ import annotations

from glassrail.core.errors import (
    GlassrailError,
    PlanRejectedError,
    PlanValidationError,
    ToolExecutionError,
    ToolRegistrationError,
)
from glassrail.core.execution import (
    BranchLogEntry,
    ExecutionState,
    NodeResult,
    NodeStatus,
    PlanningAttempt,
    TaskStatus,
)
from glassrail.core.ids import TaskId, new_task_id
from glassrail.core.plan import Node, NodeType, Plan, SummaryFormat

__all__ = [
    "BranchLogEntry",
    "ExecutionState",
    "GlassrailError",
    "Node",
    "NodeResult",
    "NodeStatus",
    "NodeType",
    "Plan",
    "PlanRejectedError",
    "PlanValidationError",
    "PlanningAttempt",
    "SummaryFormat",
    "TaskId",
    "TaskStatus",
    "ToolExecutionError",
    "ToolRegistrationError",
    "new_task_id",
]
