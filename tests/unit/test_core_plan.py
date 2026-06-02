"""Tests for the Plan / Node domain types."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dagagent.core import Node, NodeType, Plan, SummaryFormat


def test_node_minimum_fields() -> None:
    node = Node(id=1, type=NodeType.TOOL, description="fetch calendar")
    assert node.id == 1
    assert node.type is NodeType.TOOL
    assert node.tool is None
    assert node.context_needed == []
    assert node.branches is None
    assert node.reasoning_required is False
    assert node.forced_tier is None
    assert node.format is None


def test_node_type_strenum_round_trip() -> None:
    # StrEnum: equal to its string value, accepted in either form.
    assert NodeType.TOOL == "tool"
    node = Node.model_validate({"id": 2, "type": "decision", "description": "branch"})
    assert node.type is NodeType.DECISION


def test_summary_format_round_trip() -> None:
    node = Node.model_validate(
        {"id": 4, "type": "summary", "description": "condense", "format": "verbose"}
    )
    assert node.format is SummaryFormat.VERBOSE
    dumped = node.model_dump(mode="json")
    assert dumped["format"] == "verbose"


def test_node_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        Node.model_validate({"id": 3, "type": "telepathy", "description": "?"})


def test_plan_starts_with_empty_sorted_ids() -> None:
    plan = Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="x")])
    assert plan.sorted_node_ids == []


def test_plan_serialises_round_trip() -> None:
    plan = Plan(
        nodes=[
            Node(id=1, type=NodeType.TOOL, description="a", tool="calendar_get"),
            Node(
                id=2,
                type=NodeType.DECISION,
                description="b",
                condition="any events?",
                branches={"yes": [3], "no": [4]},
                default_branch="no",
                context_needed=[1],
            ),
        ]
    )
    restored = Plan.model_validate(plan.model_dump())
    assert restored == plan
