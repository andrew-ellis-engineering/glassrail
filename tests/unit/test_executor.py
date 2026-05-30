"""Tests for the Executor."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

import pytest

from dagagent.config import NodeBudgets, Settings
from dagagent.core import (
    ExecutionState,
    Node,
    NodeStatus,
    NodeType,
    Plan,
    TaskStatus,
    new_task_id,
)
from dagagent.executor import Executor
from dagagent.harness import ToolHarness, register_builtins
from dagagent.providers import Chunk, Message, TierRouter


class _ScriptedProvider:
    """Fake provider that pops scripted responses in order."""

    def __init__(self, responses: _Sequence[str], *, tier: int = 0) -> None:
        self._responses: list[str] = list(responses)
        self._tier = tier

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def tier(self) -> int:
        return self._tier

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        del messages, json_mode, max_tokens, timeout_s
        if not self._responses:
            raise RuntimeError("Scripted provider exhausted")
        yield Chunk(text=self._responses.pop(0), tokens_used=1)


def _executor(responses: list[str], *, tier: int = 0) -> tuple[Executor, ToolHarness]:
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_ScriptedProvider(responses, tier=tier)])
    return Executor(router=router, harness=harness, settings=Settings()), harness


def _state(plan: Plan) -> ExecutionState:
    state = ExecutionState(task_id=new_task_id(), user_request="x", plan=plan)
    plan.sorted_node_ids = [n.id for n in plan.nodes]
    return state


_SHAPE_OK = json.dumps({"matches_expectation": True, "issue": None})


async def test_empty_plan_completes() -> None:
    executor, _ = _executor([])
    plan = Plan(nodes=[])
    state = _state(plan)

    result = await executor.execute(state)
    assert result.status is TaskStatus.COMPLETED
    assert result.results == {}
    assert result.final_output is None


async def test_single_tool_completes() -> None:
    executor, _ = _executor([_SHAPE_OK])
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.TOOL,
                description="get date",
                tool="calendar_get",
                args_template={"date": "2026-05-27"},
            )
        ]
    )
    state = _state(plan)

    result = await executor.execute(state)
    assert result.status is TaskStatus.COMPLETED
    node_result = result.results[1]
    assert node_result.status is NodeStatus.COMPLETED
    assert node_result.tier_used == 0
    assert node_result.execution_time_s >= 0


async def test_synthesis_extracts_final_output() -> None:
    """A synthesis node's output becomes the task's final_output."""
    synth_payload = json.dumps({"output": "the answer is 42", "confidence": 0.95})
    executor, _ = _executor([synth_payload])
    plan = Plan(
        nodes=[
            Node(id=1, type=NodeType.SYNTHESIS, description="answer"),
        ]
    )
    state = _state(plan)

    result = await executor.execute(state)
    assert result.final_output == "the answer is 42"


async def test_decision_skips_non_taken_branch() -> None:
    decision_payload = json.dumps({"branch": "yes", "confidence": 0.9})
    synth_payload = json.dumps({"output": "yes-path", "confidence": 1.0})
    executor, _ = _executor([decision_payload, synth_payload])
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.DECISION,
                description="branch on something",
                condition="is it yes?",
                branches={"yes": [2], "no": [3]},
                default_branch="no",
            ),
            Node(id=2, type=NodeType.SYNTHESIS, description="yes path"),
            Node(id=3, type=NodeType.SYNTHESIS, description="no path"),
        ]
    )
    state = _state(plan)

    result = await executor.execute(state)
    assert result.results[1].branch_taken == "yes"
    assert result.results[2].status is NodeStatus.COMPLETED
    assert result.results[3].status is NodeStatus.SKIPPED
    assert 3 in result.skipped_nodes


async def test_decision_default_branch_used_on_llm_failure() -> None:
    """If the decision call returns garbage, the default_branch is taken."""
    no_synth = json.dumps({"output": "no-path", "confidence": 1.0})
    # Bad JSON for decision; then synthesis for the default branch.
    executor, _ = _executor(["this is not json at all", no_synth])
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.DECISION,
                description="branch",
                condition="?",
                branches={"yes": [2], "no": [3]},
                default_branch="no",
            ),
            Node(id=2, type=NodeType.SYNTHESIS, description="yes path"),
            Node(id=3, type=NodeType.SYNTHESIS, description="no path"),
        ]
    )
    state = _state(plan)

    result = await executor.execute(state)
    assert result.results[1].branch_taken == "no"
    assert result.results[2].status is NodeStatus.SKIPPED
    assert result.results[3].status is NodeStatus.COMPLETED


async def test_low_confidence_flagged() -> None:
    """Completed nodes below the confidence threshold get flagged."""
    low_conf = json.dumps({"output": "shaky", "confidence": 0.1})
    executor, _ = _executor([low_conf])
    plan = Plan(nodes=[Node(id=1, type=NodeType.SYNTHESIS, description="x")])
    state = _state(plan)

    result = await executor.execute(state)
    assert result.results[1].flagged is True


@pytest.fixture
def empty_tool_executor() -> Executor:
    harness = ToolHarness()
    # Register a tool that always returns empty.

    async def always_empty() -> dict[str, object]:
        return {}

    harness.tool(name="empty_tool", description="always empty", parameters={"type": "object"})(
        always_empty
    )
    router = TierRouter([_ScriptedProvider([])])
    return Executor(router=router, harness=harness, settings=Settings())


async def test_think_completes_with_reasoning_output() -> None:
    """A THINK node's reasoning becomes its output and routes to tier 2."""
    think_payload = json.dumps(
        {"reasoning": "step 1 ... step 2 ... conclusion", "confidence": 0.85}
    )
    executor, _ = _executor([think_payload], tier=2)
    plan = Plan(nodes=[Node(id=1, type=NodeType.THINK, description="reason about x")])
    state = _state(plan)

    result = await executor.execute(state)
    node_result = result.results[1]
    assert node_result.status is NodeStatus.COMPLETED
    assert node_result.output == "step 1 ... step 2 ... conclusion"
    assert node_result.confidence == 0.85
    assert node_result.tier_used == 2


async def test_think_forced_tier_overrides_default() -> None:
    """forced_tier on a THINK node beats the type's default routing."""
    think_payload = json.dumps({"reasoning": "quick", "confidence": 0.9})
    executor, _ = _executor([think_payload])
    plan = Plan(nodes=[Node(id=1, type=NodeType.THINK, description="quick reason", forced_tier=0)])
    state = _state(plan)

    result = await executor.execute(state)
    assert result.results[1].tier_used == 0


async def test_think_failure_marks_node_failed() -> None:
    """A malformed THINK response fails the node rather than masking as empty."""
    executor, _ = _executor(["not json"], tier=2)
    plan = Plan(nodes=[Node(id=1, type=NodeType.THINK, description="x")])
    state = _state(plan)

    result = await executor.execute(state)
    assert result.results[1].status is NodeStatus.FAILED


async def test_summary_condenses_upstream_context() -> None:
    """A SUMMARY node returns the condensed text as its output at tier 0."""
    summary_payload = json.dumps({"summary": "two facts: A and B", "confidence": 0.95})
    executor, _ = _executor([summary_payload])
    plan = Plan(nodes=[Node(id=1, type=NodeType.SUMMARY, description="condense")])
    state = _state(plan)

    result = await executor.execute(state)
    node_result = result.results[1]
    assert node_result.status is NodeStatus.COMPLETED
    assert node_result.output == "two facts: A and B"
    assert node_result.tier_used == 0


async def test_summary_failure_marks_node_failed() -> None:
    executor, _ = _executor(["not json"])
    plan = Plan(nodes=[Node(id=1, type=NodeType.SUMMARY, description="x")])
    state = _state(plan)

    result = await executor.execute(state)
    assert result.results[1].status is NodeStatus.FAILED


async def test_result_node_supplies_final_output() -> None:
    """A RESULT node's output becomes the task's final_output."""
    result_payload = json.dumps({"output": "the user-facing answer", "confidence": 0.95})
    executor, _ = _executor([result_payload])
    plan = Plan(nodes=[Node(id=1, type=NodeType.RESULT, description="final answer")])
    state = _state(plan)

    out = await executor.execute(state)
    assert out.results[1].status is NodeStatus.COMPLETED
    assert out.final_output == "the user-facing answer"


async def test_result_preferred_over_synthesis_for_final_output() -> None:
    """When both kinds are present, the RESULT node wins, even if SYNTHESIS
    runs later in the plan."""
    synth_payload = json.dumps({"output": "intermediate", "confidence": 0.9})
    result_payload = json.dumps({"output": "the answer", "confidence": 0.95})
    executor, _ = _executor([synth_payload, result_payload])
    plan = Plan(
        nodes=[
            Node(id=1, type=NodeType.SYNTHESIS, description="combine"),
            Node(id=2, type=NodeType.RESULT, description="final", context_needed=[1]),
        ]
    )
    state = _state(plan)

    out = await executor.execute(state)
    assert out.final_output == "the answer"


async def test_result_failure_marks_node_failed() -> None:
    executor, _ = _executor(["not json"])
    plan = Plan(nodes=[Node(id=1, type=NodeType.RESULT, description="x")])
    state = _state(plan)

    out = await executor.execute(state)
    assert out.results[1].status is NodeStatus.FAILED


async def test_subplan_bubbles_nested_final_output() -> None:
    """A SUBPLAN node's output is the nested plan's final_output."""
    nested_payload = json.dumps({"output": "nested answer", "confidence": 0.95})
    executor, _ = _executor([nested_payload])
    nested = Plan(nodes=[Node(id=1, type=NodeType.RESULT, description="nested final")])
    nested.sorted_node_ids = [1]
    plan = Plan(
        nodes=[Node(id=1, type=NodeType.SUBPLAN, description="delegate", subplan=nested)],
    )
    state = _state(plan)

    out = await executor.execute(state)
    assert out.results[1].status is NodeStatus.COMPLETED
    assert out.results[1].output == "nested answer"


async def test_subplan_without_attached_plan_fails() -> None:
    executor, _ = _executor([])
    plan = Plan(nodes=[Node(id=1, type=NodeType.SUBPLAN, description="missing")])
    state = _state(plan)

    out = await executor.execute(state)
    assert out.results[1].status is NodeStatus.FAILED


async def test_empty_tool_result_bypasses_shape_check(empty_tool_executor: Executor) -> None:
    """Empty results are flagged as EMPTY without triggering the LLM gate."""
    plan = Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="x", tool="empty_tool")])
    state = _state(plan)

    result = await empty_tool_executor.execute(state)
    assert result.results[1].status is NodeStatus.EMPTY


class _CapturingProvider:
    """Fake provider that records the ``max_tokens`` of each call."""

    def __init__(self, responses: _Sequence[str], *, tier: int = 0) -> None:
        self._responses: list[str] = list(responses)
        self._tier = tier
        self.max_tokens_seen: list[int] = []

    @property
    def name(self) -> str:
        return "capturing"

    @property
    def tier(self) -> int:
        return self._tier

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        del messages, json_mode, timeout_s
        self.max_tokens_seen.append(max_tokens)
        yield Chunk(text=self._responses.pop(0), tokens_used=1)


async def test_content_node_uses_its_configured_budget() -> None:
    """A SUMMARY node's LLM call is capped at the configured summary budget."""
    settings = Settings(budgets=NodeBudgets(summary=7777))
    provider = _CapturingProvider([json.dumps({"summary": "x", "confidence": 0.9})])
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=settings,
    )
    state = _state(Plan(nodes=[Node(id=1, type=NodeType.SUMMARY, description="condense")]))

    await executor.execute(state)
    assert provider.max_tokens_seen == [7777]


async def test_decision_node_uses_its_configured_budget() -> None:
    """The decision micro-call is capped at the configured decision budget."""
    settings = Settings(budgets=NodeBudgets(decision=42))
    provider = _CapturingProvider([json.dumps({"branch": "no", "confidence": 0.9})])
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=settings,
    )
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.DECISION,
                description="branch",
                condition="?",
                branches={"yes": [], "no": []},
                default_branch="no",
            )
        ]
    )
    await executor.execute(_state(plan))
    assert provider.max_tokens_seen == [42]
