"""Plan and node domain types.

A :class:`Plan` is a directed acyclic graph of :class:`Node` records. Each
node declares a ``type`` (what it does), an integer ``id`` (planner-assigned),
and the set of upstream node ids whose outputs it needs (``context_needed``).
The graph is validated by :mod:`dagagent.validator`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    """Kinds of node that can appear in a plan."""

    TOOL = "tool"
    DECISION = "decision"
    SYNTHESIS = "synthesis"
    THINK = "think"
    """Explicit reasoning step. Pure LLM call, no tool, defaults to tier 2."""
    SUMMARY = "summary"
    """Condense upstream context. Cheap LLM call, defaults to tier 0."""
    RESULT = "result"
    """Explicit final-answer marker. Output becomes the task's final_output."""
    SUBPLAN = "subplan"
    """A nested plan executed inline; its final_output is this node's output."""


class SummaryFormat(StrEnum):
    """Planner hint for how much detail a summary node should preserve."""

    CONCISE = "concise"
    """1-3 sentences; enough to gate a decision or feed an intermediate node."""
    MEDIUM = "medium"
    """Balanced paragraph; the default summary shape."""
    VERBOSE = "verbose"
    """Full detail; preserves all key facts for a user-facing result."""


class Node(BaseModel):
    """A single node in a plan."""

    id: int
    type: NodeType
    description: str

    # TOOL-only
    tool: str | None = None
    args_template: dict[str, Any] | None = None

    # Inputs from upstream nodes — kept minimal to honour the
    # fresh-context-per-node invariant.
    context_needed: list[int] = Field(default_factory=list)

    # DECISION-only
    condition: str | None = None
    branches: dict[str, list[int]] | None = None
    default_branch: str | None = None

    # Routing hints — planner sets these; executor consults but is not bound by them.
    reasoning_required: bool = False
    forced_tier: int | None = None

    # SUBPLAN-only — the nested plan to execute when this node fires.
    subplan: Plan | None = None

    # SUMMARY-only — ignored by other node types.
    format: SummaryFormat = SummaryFormat.MEDIUM


class Plan(BaseModel):
    """A validated execution graph."""

    nodes: list[Node]
    sorted_node_ids: list[int] = Field(default_factory=list)
    """Topologically sorted node ids, populated by the validator."""


# Resolve the forward reference: Node.subplan -> Plan (defined just above).
Node.model_rebuild()
