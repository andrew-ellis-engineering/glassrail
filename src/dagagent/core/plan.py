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
    """Kinds of node that can appear in a plan.

    The remaining extended types (``result``, ``subplan``) land alongside
    the executor and validator support for each.
    """

    TOOL = "tool"
    DECISION = "decision"
    SYNTHESIS = "synthesis"
    THINK = "think"
    """Explicit reasoning step. Pure LLM call, no tool, defaults to tier 2."""
    SUMMARY = "summary"
    """Condense upstream context. Cheap LLM call, defaults to tier 0."""


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


class Plan(BaseModel):
    """A validated execution graph."""

    nodes: list[Node]
    sorted_node_ids: list[int] = Field(default_factory=list)
    """Topologically sorted node ids, populated by the validator."""
