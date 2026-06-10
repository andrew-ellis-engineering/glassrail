"""Project-wide exception hierarchy.

Every error raised by ``glassrail`` inherits from :class:`GlassrailError`, so
callers can catch the whole family with one ``except``.
"""

from __future__ import annotations


class GlassrailError(Exception):
    """Base class for every glassrail-raised error."""


class PlanValidationError(GlassrailError):
    """Raised when a plan fails structural validation."""


class PlanRejectedError(GlassrailError):
    """Raised when the planner explicitly rejects a task it cannot complete."""


class ToolRegistrationError(GlassrailError):
    """Raised when tool registration fails (e.g., name collision)."""


class ToolExecutionError(GlassrailError):
    """Raised when a tool invocation fails at execution time."""
