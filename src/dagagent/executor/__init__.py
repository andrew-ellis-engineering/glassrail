"""Plan execution.

- :mod:`dagagent.executor.context` builds the fresh per-node prompt context.
- :mod:`dagagent.executor.executor` walks the DAG and dispatches per-node logic.
- :mod:`dagagent.executor.orchestrator` wraps planning, HITL, execution, and persistence.
"""

from __future__ import annotations

from dagagent.config.prompts import (
    DEFAULT_DECISION_SYSTEM as DECISION_SYSTEM,
)
from dagagent.config.prompts import (
    DEFAULT_SHAPE_CHECK_SYSTEM as UNEXPECTED_RESULT_SYSTEM,
)
from dagagent.config.prompts import (
    DEFAULT_SYNTHESIS_SYSTEM as SYNTHESIS_SYSTEM,
)
from dagagent.executor.context import assemble_context
from dagagent.executor.executor import Executor
from dagagent.executor.orchestrator import Orchestrator

__all__ = [
    "DECISION_SYSTEM",
    "SYNTHESIS_SYSTEM",
    "UNEXPECTED_RESULT_SYSTEM",
    "Executor",
    "Orchestrator",
    "assemble_context",
]
