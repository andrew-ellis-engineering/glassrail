"""Tests for PlanValidator."""

from __future__ import annotations

import pytest

from dagagent.config import Settings
from dagagent.core import Node, NodeType, Plan, PlanValidationError
from dagagent.harness import ToolHarness, register_builtins
from dagagent.validator import PlanValidator


@pytest.fixture
def harness() -> ToolHarness:
    h = ToolHarness()
    register_builtins(h)
    return h


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def validator(harness: ToolHarness, settings: Settings) -> PlanValidator:
    return PlanValidator(harness=harness, settings=settings)


def _tool_node(id_: int, *, ctx: list[int] | None = None) -> Node:
    return Node(
        id=id_,
        type=NodeType.TOOL,
        description=f"node {id_}",
        tool="calendar_get",
        context_needed=ctx or [],
    )


def _decision_node(
    id_: int,
    branches: dict[str, list[int]],
    *,
    ctx: list[int] | None = None,
) -> Node:
    return Node(
        id=id_,
        type=NodeType.DECISION,
        description=f"decide {id_}",
        condition="?",
        branches=branches,
        default_branch=next(iter(branches)),
        context_needed=ctx or [],
    )


def test_simple_linear_plan(validator: PlanValidator) -> None:
    plan = Plan(
        nodes=[
            _tool_node(1),
            _tool_node(2, ctx=[1]),
            _tool_node(3, ctx=[2]),
        ]
    )
    sorted_ids = validator.validate(plan)
    assert sorted_ids == [1, 2, 3]
    assert plan.sorted_node_ids == [1, 2, 3]


def test_topological_order_deterministic(validator: PlanValidator) -> None:
    # Two roots; tie-break by ascending id.
    plan = Plan(
        nodes=[
            _tool_node(2),
            _tool_node(1),
            _tool_node(3, ctx=[1, 2]),
        ]
    )
    assert validator.validate(plan) == [1, 2, 3]


def test_cycle_raises(validator: PlanValidator) -> None:
    plan = Plan(
        nodes=[
            _tool_node(1, ctx=[2]),
            _tool_node(2, ctx=[1]),
        ]
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validator.validate(plan)


def test_unknown_tool_raises(validator: PlanValidator) -> None:
    plan = Plan(
        nodes=[Node(id=1, type=NodeType.TOOL, description="?", tool="bogus_tool")],
    )
    with pytest.raises(PlanValidationError, match="unknown tools"):
        validator.validate(plan)


def test_forced_tier_outside_configured_range_raises(validator: PlanValidator) -> None:
    plan = Plan(
        nodes=[
            Node(id=1, type=NodeType.RESULT, description="answer", forced_tier=99),
        ],
    )
    with pytest.raises(PlanValidationError, match=r"configured tier range 0\.\.3"):
        validator.validate(plan)


def test_subplan_forced_tier_outside_configured_range_raises(
    validator: PlanValidator,
) -> None:
    nested = Plan(
        nodes=[
            Node(id=1, type=NodeType.RESULT, description="nested answer", forced_tier=-1),
        ],
    )
    plan = Plan(nodes=[_subplan_node(1, nested)])
    with pytest.raises(PlanValidationError, match="forced_tier=-1"):
        validator.validate(plan)


def test_missing_context_dep_raises(validator: PlanValidator) -> None:
    plan = Plan(nodes=[_tool_node(1, ctx=[99])])
    with pytest.raises(PlanValidationError, match="doesn't exist"):
        validator.validate(plan)


def test_node_limit_raises(harness: ToolHarness) -> None:
    tight = Settings(max_plan_nodes=2)
    v = PlanValidator(harness=harness, settings=tight)
    plan = Plan(nodes=[_tool_node(i) for i in range(1, 5)])
    with pytest.raises(PlanValidationError, match="max is 2"):
        v.validate(plan)


def test_branch_reference_missing(validator: PlanValidator) -> None:
    plan = Plan(
        nodes=[
            _decision_node(1, {"yes": [99], "no": [2]}),
            _tool_node(2),
        ]
    )
    with pytest.raises(PlanValidationError, match="non-existent"):
        validator.validate(plan)


def test_decision_nesting_within_limit(validator: PlanValidator) -> None:
    # depth 2 (decision → decision → leaves), limit is 2.
    plan = Plan(
        nodes=[
            _decision_node(1, {"yes": [2], "no": [4]}),
            _decision_node(2, {"yes": [3], "no": [3]}),
            _tool_node(3),
            _tool_node(4),
        ]
    )
    sorted_ids = validator.validate(plan)
    assert set(sorted_ids) == {1, 2, 3, 4}


def _subplan_node(id_: int, nested: Plan) -> Node:
    return Node(id=id_, type=NodeType.SUBPLAN, description=f"sub {id_}", subplan=nested)


def test_subplan_validated_and_sorted(validator: PlanValidator) -> None:
    nested = Plan(nodes=[_tool_node(1), _tool_node(2, ctx=[1])])
    plan = Plan(nodes=[_subplan_node(1, nested)])

    validator.validate(plan)
    # Nested plan's sorted_node_ids gets populated as a side effect.
    assert nested.sorted_node_ids == [1, 2]


def test_subplan_missing_nested_plan_raises(validator: PlanValidator) -> None:
    plan = Plan(nodes=[Node(id=1, type=NodeType.SUBPLAN, description="no plan")])
    with pytest.raises(PlanValidationError, match="no nested plan"):
        validator.validate(plan)


def test_subplan_count_limit(harness: ToolHarness) -> None:
    settings = Settings(max_subplans_per_plan=1)
    v = PlanValidator(harness=harness, settings=settings)
    nested = Plan(nodes=[_tool_node(1)])
    plan = Plan(
        nodes=[
            _subplan_node(1, nested.model_copy(deep=True)),
            _subplan_node(2, nested.model_copy(deep=True)),
        ]
    )
    with pytest.raises(PlanValidationError, match="subplan nodes; max is 1"):
        v.validate(plan)


def test_subplan_node_limit(harness: ToolHarness) -> None:
    settings = Settings(max_subplan_nodes=2)
    v = PlanValidator(harness=harness, settings=settings)
    nested = Plan(nodes=[_tool_node(i) for i in range(1, 5)])
    plan = Plan(nodes=[_subplan_node(1, nested)])
    with pytest.raises(PlanValidationError, match="Subplan has 4 nodes; max is 2"):
        v.validate(plan)


def test_subplan_invalid_tool_propagates(validator: PlanValidator) -> None:
    nested = Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="?", tool="bogus")])
    plan = Plan(nodes=[_subplan_node(1, nested)])
    with pytest.raises(PlanValidationError, match="unknown tools"):
        validator.validate(plan)


def test_decision_nesting_exceeds_limit(harness: ToolHarness) -> None:
    shallow = Settings(max_decision_nesting_depth=1)
    v = PlanValidator(harness=harness, settings=shallow)
    plan = Plan(
        nodes=[
            _decision_node(1, {"yes": [2], "no": [4]}),
            _decision_node(2, {"yes": [3], "no": [3]}),
            _tool_node(3),
            _tool_node(4),
        ]
    )
    with pytest.raises(PlanValidationError, match="nesting depth"):
        v.validate(plan)
