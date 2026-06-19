"""Tests for the Executor."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

import pytest

from glassrail.config import (
    NodeBudgets,
    NodePrompts,
    Settings,
    ToolApprovalMode,
    ToolApprovalPolicy,
    ToolApprovalSettings,
)
from glassrail.core import (
    ExecutionState,
    Node,
    NodeStatus,
    NodeType,
    Plan,
    SummaryFormat,
    TaskStatus,
    new_task_id,
)
from glassrail.events import (
    Event,
    EventBus,
    NodeFinished,
    NodeOutputChunk,
    NodeStarted,
    TaskCompleted,
)
from glassrail.executor import Executor
from glassrail.executor.executor import JsonFieldStreamer
from glassrail.executor.tool_approval import ToolApprovalBroker
from glassrail.harness import ToolHarness, register_builtins
from glassrail.providers import Chunk, Message, TierRouter


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


async def test_independent_ready_nodes_run_concurrently() -> None:
    waiting = 0
    both_waiting = asyncio.Event()
    release = asyncio.Event()
    harness = ToolHarness()

    @harness.tool(name="root", description="root", parameters={"type": "object"})
    async def _root(**_: object) -> dict[str, str]:
        return {"root": "done"}

    async def _barrier(label: str) -> dict[str, str]:
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            both_waiting.set()
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)
        return {"branch": label}

    @harness.tool(name="left", description="left", parameters={"type": "object"})
    async def _left(**_: object) -> dict[str, str]:
        return await _barrier("left")

    @harness.tool(name="right", description="right", parameters={"type": "object"})
    async def _right(**_: object) -> dict[str, str]:
        return await _barrier("right")

    result_payload = json.dumps({"output": "done", "confidence": 1.0})
    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK] * 6 + [result_payload])]),
        harness=harness,
        settings=Settings(max_concurrent_nodes=2),
    )
    plan = Plan(
        nodes=[
            Node(id=1, type=NodeType.TOOL, description="root", tool="root"),
            Node(id=2, type=NodeType.TOOL, description="left", tool="left", context_needed=[1]),
            Node(id=3, type=NodeType.TOOL, description="right", tool="right", context_needed=[1]),
            Node(id=4, type=NodeType.RESULT, description="final", context_needed=[2, 3]),
        ]
    )

    result = await executor.execute(_state(plan))

    assert both_waiting.is_set()
    assert result.results[2].status is NodeStatus.COMPLETED
    assert result.results[3].status is NodeStatus.COMPLETED
    assert result.final_output == "done"


async def test_max_concurrent_nodes_one_preserves_sequential_order() -> None:
    order: list[str] = []
    harness = ToolHarness()

    def _register_tool(name: str) -> None:
        @harness.tool(name=name, description=name, parameters={"type": "object"})
        async def _tool(**_: object) -> dict[str, str]:
            order.append(name)
            return {"tool": name}

    _register_tool("root")
    _register_tool("left")
    _register_tool("right")
    result_payload = json.dumps({"output": "done", "confidence": 1.0})
    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK] * 6 + [result_payload])]),
        harness=harness,
        settings=Settings(max_concurrent_nodes=1),
    )
    plan = Plan(
        nodes=[
            Node(id=1, type=NodeType.TOOL, description="root", tool="root"),
            Node(id=2, type=NodeType.TOOL, description="left", tool="left", context_needed=[1]),
            Node(id=3, type=NodeType.TOOL, description="right", tool="right", context_needed=[1]),
            Node(id=4, type=NodeType.RESULT, description="final", context_needed=[2, 3]),
        ]
    )

    await executor.execute(_state(plan))

    assert order == ["root", "left", "right"]


async def test_tool_approval_deny_blocks_execution() -> None:
    calls = 0
    harness = ToolHarness()

    @harness.tool(name="danger", description="danger", parameters={"type": "object"}, risk="write")
    async def _danger() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"ok": "yes"}

    executor = Executor(
        router=TierRouter([_ScriptedProvider([])]),
        harness=harness,
        settings=Settings(
            tool_approval=ToolApprovalSettings(overrides={"danger": ToolApprovalPolicy.DENY})
        ),
    )
    state = _state(
        Plan(
            nodes=[
                Node(id=1, type=NodeType.TOOL, description="run danger", tool="danger"),
            ]
        )
    )

    result = await executor.execute(state)

    assert calls == 0
    assert result.results[1].status is NodeStatus.FAILED
    assert "user_denied" in (result.results[1].error or "")


async def test_tool_approval_ask_uses_broker() -> None:
    bus = EventBus()
    broker = ToolApprovalBroker(bus)
    harness = ToolHarness()

    @harness.tool(name="writer", description="write", parameters={"type": "object"}, risk="write")
    async def _writer() -> dict[str, str]:
        return {"written": "yes"}

    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK])]),
        harness=harness,
        settings=Settings(
            tool_approval=ToolApprovalSettings(overrides={"writer": ToolApprovalPolicy.ASK})
        ),
        event_bus=bus,
        tool_approval=broker,
    )
    state = _state(Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="write", tool="writer")]))

    async with bus.subscribe() as sub:
        task = asyncio.create_task(executor.execute(state))
        event = await sub.__anext__()
        while event.type != "tool_approval_requested":
            event = await sub.__anext__()
        assert event.tool_name == "writer"
        broker.resolve(event.approval_id, True)
        result = await task

    assert result.results[1].status is NodeStatus.COMPLETED


async def test_write_risk_tool_asks_by_default_in_interactive_mode() -> None:
    bus = EventBus()
    broker = ToolApprovalBroker(bus)
    harness = ToolHarness()

    @harness.tool(name="writer", description="write", parameters={"type": "object"}, risk="write")
    async def _writer() -> dict[str, str]:
        return {"written": "yes"}

    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK])]),
        harness=harness,
        settings=Settings(),
        event_bus=bus,
        tool_approval=broker,
    )
    state = _state(Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="write", tool="writer")]))

    async with bus.subscribe() as sub:
        task = asyncio.create_task(executor.execute(state))
        event = await sub.__anext__()
        while event.type != "tool_approval_requested":
            event = await sub.__anext__()
        assert event.tool_name == "writer"
        assert event.risk == "write"
        broker.resolve(event.approval_id, True)
        result = await task

    assert result.results[1].status is NodeStatus.COMPLETED


async def test_write_risk_tool_runs_without_prompt_in_auto_mode() -> None:
    calls = 0
    harness = ToolHarness()

    @harness.tool(name="writer", description="write", parameters={"type": "object"}, risk="write")
    async def _writer() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"written": "yes"}

    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK])]),
        harness=harness,
        settings=Settings(
            tool_approval=ToolApprovalSettings(
                mode=ToolApprovalMode.AUTO,
            )
        ),
    )
    state = _state(Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="write", tool="writer")]))

    result = await executor.execute(state)

    assert calls == 1
    assert result.results[1].status is NodeStatus.COMPLETED


async def test_explicit_allow_override_bypasses_write_risk_prompt() -> None:
    calls = 0
    harness = ToolHarness()

    @harness.tool(name="writer", description="write", parameters={"type": "object"}, risk="write")
    async def _writer() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"written": "yes"}

    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK])]),
        harness=harness,
        settings=Settings(
            tool_approval=ToolApprovalSettings(
                overrides={"writer": ToolApprovalPolicy.ALLOW},
            )
        ),
    )
    state = _state(Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="write", tool="writer")]))

    result = await executor.execute(state)

    assert calls == 1
    assert result.results[1].status is NodeStatus.COMPLETED


async def test_read_risk_tool_follows_default_policy() -> None:
    bus = EventBus()
    broker = ToolApprovalBroker(bus)
    harness = ToolHarness()

    @harness.tool(name="reader", description="read", parameters={"type": "object"}, risk="read")
    async def _reader() -> dict[str, str]:
        return {"read": "yes"}

    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK])]),
        harness=harness,
        settings=Settings(
            tool_approval=ToolApprovalSettings(
                default=ToolApprovalPolicy.ASK,
            )
        ),
        event_bus=bus,
        tool_approval=broker,
    )
    state = _state(Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="read", tool="reader")]))

    async with bus.subscribe() as sub:
        task = asyncio.create_task(executor.execute(state))
        event = await sub.__anext__()
        while event.type != "tool_approval_requested":
            event = await sub.__anext__()
        assert event.tool_name == "reader"
        assert event.risk == "read"
        broker.resolve(event.approval_id, True)
        result = await task

    assert result.results[1].status is NodeStatus.COMPLETED


async def test_tool_approval_auto_mode_allows_ask_but_not_deny() -> None:
    calls = 0
    harness = ToolHarness()

    @harness.tool(name="maybe", description="maybe", parameters={"type": "object"}, risk="write")
    async def _maybe() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"ok": "yes"}

    ask_settings = Settings(
        tool_approval=ToolApprovalSettings(
            mode=ToolApprovalMode.AUTO,
            overrides={"maybe": ToolApprovalPolicy.ASK},
        )
    )
    executor = Executor(
        router=TierRouter([_ScriptedProvider([_SHAPE_OK])]),
        harness=harness,
        settings=ask_settings,
    )
    ask_state = _state(
        Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="maybe", tool="maybe")])
    )
    await executor.execute(ask_state)
    assert calls == 1

    deny_settings = Settings(
        tool_approval=ToolApprovalSettings(
            mode=ToolApprovalMode.AUTO,
            overrides={"maybe": ToolApprovalPolicy.DENY},
        )
    )
    deny_executor = Executor(
        router=TierRouter([_ScriptedProvider([])]),
        harness=harness,
        settings=deny_settings,
    )
    deny_state = _state(
        Plan(nodes=[Node(id=1, type=NodeType.TOOL, description="maybe", tool="maybe")])
    )
    result = await deny_executor.execute(deny_state)
    assert calls == 1
    assert result.results[1].status is NodeStatus.FAILED


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


async def test_final_output_ignores_result_with_only_skipped_content() -> None:
    decision_payload = json.dumps({"branch": "yes", "confidence": 0.9})
    yes_payload = json.dumps({"reasoning": "half is 123", "confidence": 1.0})
    result_payload = json.dumps({"output": "123", "confidence": 1.0})
    executor, _ = _executor(
        [decision_payload, yes_payload, result_payload],
        tier=2,
    )
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.DECISION,
                description="branch on parity",
                condition="Is 246 even?",
                branches={"yes": [2], "no": [3]},
                default_branch="yes",
            ),
            Node(id=2, type=NodeType.THINK, description="calculate half"),
            Node(id=3, type=NodeType.THINK, description="calculate next even"),
            Node(id=4, type=NodeType.RESULT, description="report half", context_needed=[2]),
            Node(id=5, type=NodeType.RESULT, description="report next even", context_needed=[3]),
        ]
    )
    state = _state(plan)

    result = await executor.execute(state)

    assert result.results[2].status is NodeStatus.COMPLETED
    assert result.results[3].status is NodeStatus.SKIPPED
    assert result.results[4].status is NodeStatus.COMPLETED
    assert result.results[5].status is NodeStatus.SKIPPED
    assert result.final_output == "123"


async def test_decision_preserves_shared_join_after_branch_skip() -> None:
    decision_payload = json.dumps({"branch": "yes", "confidence": 0.9})
    yes_payload = json.dumps({"output": "yes-path", "confidence": 1.0})
    join_payload = json.dumps({"output": "final from yes-path", "confidence": 1.0})
    executor, _ = _executor([decision_payload, yes_payload, join_payload])
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.DECISION,
                description="branch",
                condition="is it yes?",
                branches={"yes": [2], "no": [3]},
                default_branch="yes",
            ),
            Node(id=2, type=NodeType.SYNTHESIS, description="yes path"),
            Node(id=3, type=NodeType.SYNTHESIS, description="no path"),
            Node(id=4, type=NodeType.RESULT, description="join", context_needed=[2, 3]),
        ]
    )
    state = _state(plan)

    result = await executor.execute(state)

    assert result.results[3].status is NodeStatus.SKIPPED
    assert result.results[4].status is NodeStatus.COMPLETED
    assert result.final_output == "final from yes-path"


async def test_decision_skips_downstream_node_that_only_uses_skipped_content() -> None:
    decision_payload = json.dumps({"branch": "yes", "confidence": 0.9})
    yes_payload = json.dumps({"output": "yes-path", "confidence": 1.0})
    executor, _ = _executor([decision_payload, yes_payload])
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.DECISION,
                description="branch",
                condition="is it yes?",
                branches={"yes": [2], "no": [3]},
                default_branch="yes",
            ),
            Node(id=2, type=NodeType.SYNTHESIS, description="yes path"),
            Node(id=3, type=NodeType.SYNTHESIS, description="no path"),
            Node(id=4, type=NodeType.RESULT, description="depends only on no", context_needed=[3]),
        ]
    )

    result = await executor.execute(_state(plan))

    assert result.results[3].status is NodeStatus.SKIPPED
    assert result.results[4].status is NodeStatus.SKIPPED
    assert 4 in result.skipped_nodes


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


async def test_summary_falls_back_when_result_is_skipped() -> None:
    decision_payload = json.dumps({"branch": "yes", "confidence": 0.9})
    summary_payload = json.dumps({"summary": "good summary", "confidence": 0.95})
    executor, _ = _executor([decision_payload, summary_payload])
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.DECISION,
                description="branch",
                condition="has content?",
                branches={"yes": [2], "no": [3]},
                default_branch="yes",
            ),
            Node(id=2, type=NodeType.SUMMARY, description="summarize"),
            Node(id=3, type=NodeType.RESULT, description="empty file"),
        ]
    )
    state = _state(plan)

    out = await executor.execute(state)

    assert out.results[3].status is NodeStatus.SKIPPED
    assert out.final_output == "good summary"


async def test_terminal_think_does_not_supply_final_output() -> None:
    think_payload = json.dumps({"reasoning": "Alice owns the fish", "confidence": 0.95})
    executor, _ = _executor([think_payload], tier=2)
    plan = Plan(nodes=[Node(id=1, type=NodeType.THINK, description="solve logic puzzle")])
    state = _state(plan)

    out = await executor.execute(state)

    assert out.results[1].status is NodeStatus.COMPLETED
    assert out.final_output is None


async def test_result_failure_marks_node_failed() -> None:
    executor, _ = _executor(["not json"])
    plan = Plan(nodes=[Node(id=1, type=NodeType.RESULT, description="x")])
    state = _state(plan)

    out = await executor.execute(state)
    assert out.results[1].status is NodeStatus.FAILED


async def test_result_failure_retries_next_configured_tier() -> None:
    low = _ScriptedProvider(["not json"], tier=0)
    high = _ScriptedProvider(
        [json.dumps({"output": "recovered answer", "confidence": 0.95})], tier=1
    )
    executor = Executor(
        router=TierRouter([low, high]),
        harness=ToolHarness(),
        settings=Settings(),
    )
    plan = Plan(nodes=[Node(id=1, type=NodeType.RESULT, description="x")])
    state = _state(plan)

    out = await executor.execute(state)

    assert out.results[1].status is NodeStatus.COMPLETED
    assert out.results[1].tier_used == 1
    assert out.final_output == "recovered answer"


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


async def test_subplan_emits_nested_events_with_node_path() -> None:
    nested_payload = json.dumps({"output": "nested answer", "confidence": 0.95})
    bus = EventBus()
    executor = Executor(
        router=TierRouter([_ScriptedProvider([nested_payload])]),
        harness=ToolHarness(),
        settings=Settings(),
        event_bus=bus,
    )
    nested = Plan(nodes=[Node(id=2, type=NodeType.RESULT, description="nested final")])
    nested.sorted_node_ids = [2]
    plan = Plan(
        nodes=[Node(id=4, type=NodeType.SUBPLAN, description="delegate", subplan=nested)],
    )
    state = _state(plan)

    async with bus.subscribe() as sub:
        await executor.execute(state)
        events: list[Event] = []
        while True:
            event = await asyncio.wait_for(anext(sub), timeout=1)
            events.append(event)
            if isinstance(event, TaskCompleted):
                break

    started = [e for e in events if isinstance(e, NodeStarted)]
    finished = [e for e in events if isinstance(e, NodeFinished)]
    completed = [e for e in events if isinstance(e, TaskCompleted)]

    assert [(e.node_id, e.node_path) for e in started] == [(4, None), (2, "4/2")]
    assert [(e.node_id, e.node_path) for e in finished] == [(2, "4/2"), (4, None)]
    assert len(completed) == 1


async def test_subplan_request_preserves_parent_task_instructions() -> None:
    nested_payload = json.dumps({"output": "DuckDB summary", "confidence": 0.95})
    provider = _CapturingProvider([nested_payload])
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=Settings(),
    )
    nested = Plan(nodes=[Node(id=1, type=NodeType.RESULT, description="Summarize DuckDB")])
    nested.sorted_node_ids = [1]
    plan = Plan(
        nodes=[
            Node(
                id=1,
                type=NodeType.SUBPLAN,
                description="Research DuckDB for local analytics",
                subplan=nested,
            )
        ],
    )
    state = _state(plan)
    state.user_request = (
        "Compare SQLite, DuckDB, and Parquet. Use stable local-analytics "
        "knowledge; do not require web research."
    )

    await executor.execute(state)

    assert provider.user_seen
    nested_result_prompt = provider.user_seen[0]
    assert "Original user request:" in nested_result_prompt
    assert "Subplan task: Research DuckDB for local analytics" in nested_result_prompt
    assert "Parent task:" in nested_result_prompt
    assert "Use stable local-analytics knowledge" in nested_result_prompt


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
        self.system_seen: list[str] = []
        self.user_seen: list[str] = []

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
        del json_mode, timeout_s
        self.max_tokens_seen.append(max_tokens)
        self.system_seen.append(next(m["content"] for m in messages if m["role"] == "system"))
        self.user_seen.append(next(m["content"] for m in messages if m["role"] == "user"))
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


async def test_content_node_uses_configured_prompt() -> None:
    """A custom summary prompt is sent as the SUMMARY node's system message."""
    settings = Settings(prompts=NodePrompts(summary="CUSTOM SUMMARY PROMPT"))
    provider = _CapturingProvider([json.dumps({"summary": "x", "confidence": 0.9})])
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=settings,
    )
    state = _state(Plan(nodes=[Node(id=1, type=NodeType.SUMMARY, description="condense")]))

    await executor.execute(state)
    assert provider.system_seen == ["CUSTOM SUMMARY PROMPT"]


async def test_summary_format_selects_variant_prompts() -> None:
    """Concise and verbose summary nodes use distinct system prompts."""
    provider = _CapturingProvider(
        [
            json.dumps({"summary": "short", "confidence": 0.9}),
            json.dumps({"summary": "long", "confidence": 0.9}),
        ]
    )
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=Settings(),
    )
    state = _state(
        Plan(
            nodes=[
                Node(
                    id=1,
                    type=NodeType.SUMMARY,
                    description="condense for decision",
                    format=SummaryFormat.CONCISE,
                ),
                Node(
                    id=2,
                    type=NodeType.SUMMARY,
                    description="condense for final answer",
                    format=SummaryFormat.VERBOSE,
                ),
            ]
        )
    )

    await executor.execute(state)

    assert len(provider.system_seen) == 2
    assert "concise 1-3 sentence summary" in provider.system_seen[0]
    assert "thorough summary preserving all key facts" in provider.system_seen[1]
    assert provider.system_seen[0] != provider.system_seen[1]


async def test_decision_context_includes_direct_dependents() -> None:
    """Decision prompts include downstream descriptions without leaking extra results."""
    provider = _CapturingProvider(
        [
            json.dumps({"branch": "yes", "confidence": 0.9}),
            json.dumps({"output": "yes", "confidence": 0.9}),
        ]
    )
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=Settings(),
    )
    state = _state(
        Plan(
            nodes=[
                Node(
                    id=1,
                    type=NodeType.DECISION,
                    description="choose a path",
                    condition="is the signal positive?",
                    branches={"yes": [2], "no": []},
                    default_branch="no",
                ),
                Node(
                    id=2,
                    type=NodeType.SYNTHESIS,
                    description="write the positive-path response",
                    context_needed=[1],
                ),
            ]
        )
    )

    await executor.execute(state)

    decision_msg = provider.user_seen[0]
    assert "Your output will be consumed by" in decision_msg
    assert "Node 2" in decision_msg
    assert "write the positive-path response" in decision_msg


# ── JsonFieldStreamer unit tests ─────────────────────────────────────────────


def test_streamer_extracts_full_value_in_one_chunk() -> None:
    s = JsonFieldStreamer("reasoning")
    result = s.feed('{"reasoning": "hello world", "confidence": 0.7}')
    assert result == "hello world"
    assert s.done


def test_streamer_extracts_value_across_chunks() -> None:
    s = JsonFieldStreamer("reasoning")
    out = ""
    for chunk in ['{"reas', 'oning"', ': "hel', "lo wor", 'ld"}']:
        out += s.feed(chunk)
    assert out == "hello world"
    assert s.done


def test_streamer_marker_straddles_chunks() -> None:
    """The key-colon-quote marker itself may be split across two chunks."""
    s = JsonFieldStreamer("reasoning")
    out = s.feed('{"reas')
    assert out == ""
    out += s.feed('oning": "content"}')
    assert out == "content"


def test_streamer_handles_escaped_quote() -> None:
    s = JsonFieldStreamer("reasoning")
    result = s.feed('{"reasoning": "say \\"hi\\"", "confidence": 0.5}')
    assert result == 'say "hi"'
    assert s.done


def test_streamer_handles_escaped_newline_and_tab() -> None:
    s = JsonFieldStreamer("reasoning")
    result = s.feed('{"reasoning": "line1\\nline2\\tend"}')
    assert result == "line1\nline2\tend"
    assert s.done


def test_streamer_no_space_after_colon() -> None:
    """JSON without a space between : and the opening quote is also valid."""
    s = JsonFieldStreamer("reasoning")
    result = s.feed('{"reasoning":"compact"}')
    assert result == "compact"
    assert s.done


def test_streamer_field_not_present_returns_empty() -> None:
    s = JsonFieldStreamer("reasoning")
    result = s.feed('{"output": "something else"}')
    assert result == ""
    assert not s.done


def test_streamer_handles_unicode_escape() -> None:
    s = JsonFieldStreamer("reasoning")
    result = s.feed('{"reasoning": "caf\\u00e9"}')
    assert result == "café"
    assert s.done


def test_streamer_handles_unicode_escape_across_chunks() -> None:
    s = JsonFieldStreamer("reasoning")
    out = s.feed('{"reasoning": "a\\u00')
    out += s.feed('41b"}')
    assert out == "aAb"


def test_streamer_returns_empty_after_done() -> None:
    s = JsonFieldStreamer("k")
    s.feed('{"k": "done"}')
    assert s.done
    assert s.feed('{"k": "more"}') == ""


# ── NodeOutputChunk streaming integration ────────────────────────────────────


class _ChunkingProvider:
    """Fake provider that emits a response one character at a time."""

    def __init__(self, response: str, *, tier: int = 0) -> None:
        self._response = response
        self._tier = tier

    @property
    def name(self) -> str:
        return "chunking"

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
        for char in self._response[:-1]:
            yield Chunk(text=char)
        yield Chunk(text=self._response[-1], tokens_used=len(self._response))


async def test_think_node_emits_output_chunks() -> None:
    """A THINK node emits NodeOutputChunk events as the response streams in."""
    payload = json.dumps({"reasoning": "step one two three", "confidence": 0.8})
    provider = _ChunkingProvider(payload, tier=2)
    bus = EventBus()
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=Settings(),
        event_bus=bus,
    )
    plan = Plan(nodes=[Node(id=1, type=NodeType.THINK, description="reason")])
    state = _state(plan)

    chunks: list[str] = []
    async with bus.subscribe() as sub:

        async def collect_think() -> None:
            async for event in sub:
                if isinstance(event, NodeOutputChunk):
                    assert event.node_type is NodeType.THINK
                    chunks.append(event.text)
                elif isinstance(event, TaskCompleted):
                    break

        await asyncio.gather(executor.execute(state), collect_think())

    assert "".join(chunks) == "step one two three"


async def test_synthesis_node_emits_output_chunks() -> None:
    """A SYNTHESIS node also emits streaming NodeOutputChunk events."""
    payload = json.dumps({"output": "the result", "confidence": 0.9})
    provider = _ChunkingProvider(payload)
    bus = EventBus()
    executor = Executor(
        router=TierRouter([provider]),
        harness=ToolHarness(),
        settings=Settings(),
        event_bus=bus,
    )
    plan = Plan(nodes=[Node(id=1, type=NodeType.SYNTHESIS, description="combine")])
    state = _state(plan)

    chunks: list[str] = []
    async with bus.subscribe() as sub:

        async def collect_synth() -> None:
            async for event in sub:
                if isinstance(event, NodeOutputChunk):
                    assert event.node_type is NodeType.SYNTHESIS
                    chunks.append(event.text)
                elif isinstance(event, TaskCompleted):
                    break

        await asyncio.gather(executor.execute(state), collect_synth())

    assert "".join(chunks) == "the result"


async def test_result_node_does_not_emit_output_chunks() -> None:
    """RESULT nodes are not streamed; their output surfaces via TaskCompleted."""
    payload = json.dumps({"output": "final answer", "confidence": 0.95})
    bus = EventBus()
    executor = Executor(
        router=TierRouter([_ChunkingProvider(payload)]),
        harness=ToolHarness(),
        settings=Settings(),
        event_bus=bus,
    )
    plan = Plan(nodes=[Node(id=1, type=NodeType.RESULT, description="final")])
    state = _state(plan)

    seen_output_chunk = False
    async with bus.subscribe() as sub:

        async def collect_events() -> None:
            nonlocal seen_output_chunk
            async for event in sub:
                if isinstance(event, NodeOutputChunk):
                    seen_output_chunk = True
                if isinstance(event, TaskCompleted):
                    break

        await asyncio.gather(executor.execute(state), collect_events())

    assert not seen_output_chunk, "RESULT nodes must not emit NodeOutputChunk"
