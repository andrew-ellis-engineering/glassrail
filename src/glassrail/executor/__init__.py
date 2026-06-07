"""Plan execution.

- :mod:`glassrail.executor.context` builds the fresh per-node prompt context.
- :mod:`glassrail.executor.executor` walks the DAG and dispatches per-node logic.
- :mod:`glassrail.executor.orchestrator` wraps planning, HITL, execution, and persistence.
"""

from __future__ import annotations

from glassrail.config.prompts import (
    DEFAULT_DECISION_SYSTEM as DECISION_SYSTEM,
)
from glassrail.config.prompts import (
    DEFAULT_SHAPE_CHECK_SYSTEM as UNEXPECTED_RESULT_SYSTEM,
)
from glassrail.config.prompts import (
    DEFAULT_SYNTHESIS_SYSTEM as SYNTHESIS_SYSTEM,
)
from glassrail.executor.context import assemble_context
from glassrail.executor.executor import Executor
from glassrail.executor.orchestrator import Orchestrator

__all__ = [
    "DECISION_SYSTEM",
    "SYNTHESIS_SYSTEM",
    "UNEXPECTED_RESULT_SYSTEM",
    "Executor",
    "Orchestrator",
    "assemble_context",
]
