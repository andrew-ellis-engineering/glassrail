"""Core domain types shared by every other subpackage.

Importing from ``dagagent.core`` is the canonical way to refer to these types.
Nothing in ``dagagent.core`` may import from any other ``dagagent`` subpackage.
"""

from __future__ import annotations

from dagagent.core.errors import (
    DagagentError,
    PlanRejectedError,
    PlanValidationError,
    ToolExecutionError,
    ToolRegistrationError,
)
from dagagent.core.execution import (
    BranchLogEntry,
    ExecutionState,
    NodeResult,
    NodeStatus,
    PlanningAttempt,
    TaskStatus,
)
from dagagent.core.ids import TaskId, new_task_id
from dagagent.core.plan import Node, NodeType, Plan, SummaryFormat

__all__ = [
    "BranchLogEntry",
    "DagagentError",
    "ExecutionState",
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
