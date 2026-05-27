"""Tests for the Executor."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

import pytest

from dagagent.config import Settings
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

    def __init__(self, responses: _Sequence[str]) -> None:
        self._responses: list[str] = list(responses)

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def tier(self) -> int:
        return 0

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


def _executor(responses: list[str]) -> tuple[Executor, ToolHarness]:
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_ScriptedProvider(responses)])
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


async def test_empty_tool_result_bypasses_shape_check(empty_tool_executor: Executor) -> None:
    """Empty results are flagged as EMPTY without triggering the LLM gate."""
    plan = Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="x", tool="empty_tool")])
    state = _state(plan)

    result = await empty_tool_executor.execute(state)
    assert result.results[1].status is NodeStatus.EMPTY
