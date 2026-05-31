"""Project-wide exception hierarchy.

Every error raised by ``dagagent`` inherits from :class:`DagagentError`, so
callers can catch the whole family with one ``except``.
"""

from __future__ import annotations


class DagagentError(Exception):
    """Base class for every dagagent-raised error."""


class PlanValidationError(DagagentError):
    """Raised when a plan fails structural validation."""


class PlanRejectedError(DagagentError):
    """Raised when the planner explicitly rejects a task it cannot complete."""


class ToolRegistrationError(DagagentError):
    """Raised when tool registration fails (e.g., name collision)."""


class ToolExecutionError(DagagentError):
    """Raised when a tool invocation fails at execution time."""
