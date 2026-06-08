"""Integration tests for the Orchestrator.

End-to-end-ish: real Planner, real Executor, real PlanValidator, real
ToolHarness, real InMemoryStateStore, fake LLM provider scripted with
the responses the prompt would normally elicit.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

from glassrail.config import NodeBudgets, Settings
from glassrail.core import ExecutionState, TaskStatus, new_task_id
from glassrail.events import EventBus
from glassrail.executor import Executor, Orchestrator
from glassrail.harness import ToolHarness, register_builtins
from glassrail.planner import Planner
from glassrail.providers import Chunk, Message, TierRouter
from glassrail.state import InMemoryStateStore
from glassrail.validator import PlanValidator

_REJECTION_PAYLOAD = json.dumps({"rejection": "I don't have a send_email tool"})


class _Scripted:
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
            raise RuntimeError("scripted exhausted")
        yield Chunk(text=self._responses.pop(0), tokens_used=1)


class _CapturingScripted(_Scripted):
    """Like _Scripted but records every user message it receives."""

    def __init__(self, responses: _Sequence[str]) -> None:
        super().__init__(responses)
        self.user_messages: list[str] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
        self.user_messages.append(user_msg)
        async for chunk in super().complete(
            messages, json_mode=json_mode, max_tokens=max_tokens, timeout_s=timeout_s
        ):
            yield chunk


def _build_capturing(
    responses: list[str],
    *,
    settings: Settings | None = None,
) -> tuple[Orchestrator, InMemoryStateStore, _CapturingScripted]:
    settings = settings or Settings()
    harness = ToolHarness()
    register_builtins(harness)
    provider = _CapturingScripted(responses)
    router = TierRouter([provider])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator, settings=settings)
    executor = Executor(router=router, harness=harness, settings=settings)
    store = InMemoryStateStore()
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
    )
    return orchestrator, store, provider


def _build(
    responses: list[str],
    *,
    settings: Settings | None = None,
) -> tuple[Orchestrator, InMemoryStateStore]:
    settings = settings or Settings()
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_Scripted(responses)])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator, settings=settings)
    executor = Executor(router=router, harness=harness, settings=settings)
    store = InMemoryStateStore()
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
    )
    return orchestrator, store


_PLAN_PAYLOAD = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "tool",
                "description": "get today",
                "tool": "calendar_get",
                "args_template": {"date": "2026-05-27"},
                "context_needed": [],
            },
            {
                "id": 2,
                "type": "synthesis",
                "description": "summarise",
                "context_needed": [1],
            },
            {
                "id": 3,
                "type": "result",
                "description": "final answer",
                "context_needed": [2],
            },
        ]
    }
)
_SHAPE_OK = json.dumps({"matches_expectation": True, "issue": None})
_SYNTH_OUT = json.dumps({"output": "nothing scheduled.", "confidence": 0.9})
_RESULT_OUT = json.dumps({"output": "nothing scheduled.", "confidence": 0.9})


async def _seed_task(
    store: InMemoryStateStore,
    request: str = "what do I have today?",
) -> ExecutionState:
    state = ExecutionState(task_id=new_task_id(), user_request=request)
    await store.save_task(state)
    return state


async def test_full_flow_plan_validate_execute() -> None:
    orch, store = _build([_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT])
    state = await _seed_task(store)

    await orch.run(state.task_id)
    stored = await store.load_task(state.task_id)

    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED
    assert stored.plan is not None
    assert len(stored.plan.nodes) == 3
    assert stored.final_output == "nothing scheduled."


async def test_confirm_gate_pauses_and_resume_finishes() -> None:
    orch, store = _build(
        [_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT],
        settings=Settings(confirm_plans=True),
    )
    state = await _seed_task(store)

    await orch.run(state.task_id)
    paused = await store.load_task(state.task_id)
    assert paused is not None
    assert paused.status is TaskStatus.AWAITING_CONFIRMATION
    assert paused.plan is not None

    await orch.resume(state.task_id)
    done = await store.load_task(state.task_id)
    assert done is not None
    assert done.status is TaskStatus.COMPLETED
    assert done.final_output == "nothing scheduled."


_PLAN_REVISED = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "tool",
                "description": "get today",
                "tool": "calendar_get",
                "args_template": {"date": "2026-05-27"},
                "context_needed": [],
            },
            {
                "id": 2,
                "type": "synthesis",
                "description": "revised summary in bullet points",
                "context_needed": [1],
            },
            {
                "id": 3,
                "type": "result",
                "description": "final answer in bullet points",
                "context_needed": [2],
            },
        ]
    }
)


async def test_revise_replans_and_re_enters_the_gate() -> None:
    orch, store = _build(
        [_PLAN_PAYLOAD, _PLAN_REVISED, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT],
        settings=Settings(confirm_plans=True),
    )
    state = await _seed_task(store)

    await orch.run(state.task_id)
    paused = await store.load_task(state.task_id)
    assert paused is not None
    assert paused.status is TaskStatus.AWAITING_CONFIRMATION
    assert paused.plan is not None
    assert paused.plan.nodes[1].description == "summarise"

    await orch.revise(state.task_id, "use bullet points")
    repaused = await store.load_task(state.task_id)
    assert repaused is not None
    # Re-planned with the new payload, and paused at the gate again.
    assert repaused.status is TaskStatus.AWAITING_CONFIRMATION
    assert repaused.plan is not None
    assert repaused.plan.nodes[1].description == "revised summary in bullet points"

    # The revised plan can then be approved and executed.
    await orch.resume(state.task_id)
    done = await store.load_task(state.task_id)
    assert done is not None
    assert done.status is TaskStatus.COMPLETED
    assert done.final_output == "nothing scheduled."


async def test_revise_on_non_paused_task_is_noop() -> None:
    orch, store = _build([])
    state = await _seed_task(store)
    await orch.revise(state.task_id, "feedback")
    reloaded = await store.load_task(state.task_id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.PLANNING


async def test_failed_planning_marks_task_failed() -> None:
    orch, store = _build(["this isn't even close to JSON", "still not JSON"])
    state = await _seed_task(store)

    await orch.run(state.task_id)
    failed = await store.load_task(state.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error is not None
    assert "Planning failed" in failed.error
    assert len(failed.planning_attempts) == 2
    assert failed.planning_attempts[0].raw_output == "this isn't even close to JSON"
    assert failed.planning_attempts[0].error_type == "json"


async def test_resume_on_non_paused_task_is_noop() -> None:
    orch, store = _build([])
    state = await _seed_task(store)
    await orch.resume(state.task_id)
    reloaded = await store.load_task(state.task_id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.PLANNING


async def test_missing_task_is_noop() -> None:
    orch, _ = _build([])
    await orch.run(new_task_id())
    # Just verifying it doesn't raise.


def _build_with_bus(
    responses: list[str],
    *,
    settings: Settings | None = None,
) -> tuple[Orchestrator, InMemoryStateStore, EventBus]:
    settings = settings or Settings()
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_Scripted(responses)])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator, settings=settings)
    executor = Executor(router=router, harness=harness, settings=settings)
    store = InMemoryStateStore()
    bus = EventBus()
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
        event_bus=bus,
    )
    return orchestrator, store, bus


async def test_rejection_marks_task_rejected_and_does_not_retry() -> None:
    # The scripted provider only has one response; if it were retried a second
    # time the _Scripted would raise RuntimeError("scripted exhausted").
    orch, store = _build([_REJECTION_PAYLOAD])
    state = await _seed_task(store, "send an email to nobody")

    await orch.run(state.task_id)
    result = await store.load_task(state.task_id)

    assert result is not None
    assert result.status is TaskStatus.REJECTED
    assert result.error == "I don't have a send_email tool"
    assert len(result.planning_attempts) == 1
    assert result.planning_attempts[0].error_type == "rejection"


async def test_rejection_emits_plan_rejected_event() -> None:
    orch, store, bus = _build_with_bus([_REJECTION_PAYLOAD])
    state = await _seed_task(store, "send an email to nobody")

    seen: list[str] = []

    async def collect_events() -> None:
        async with bus.subscribe() as sub:
            async for event in sub:
                seen.append(event.type)
                if event.type in {"plan_rejected", "plan_failed", "task_failed"}:
                    return

    collector = asyncio.create_task(collect_events())
    run_task = asyncio.create_task(orch.run(state.task_id))
    # Let the collector subscribe before events are published.
    await asyncio.sleep(0)
    await asyncio.wait_for(run_task, timeout=5)
    await asyncio.wait_for(collector, timeout=1)

    assert "plan_rejected" in seen
    assert "plan_failed" not in seen


async def test_revise_rejection_marks_task_rejected() -> None:
    orch, store = _build(
        [_PLAN_PAYLOAD, _REJECTION_PAYLOAD],
        settings=Settings(confirm_plans=True),
    )
    state = await _seed_task(store)

    await orch.run(state.task_id)
    paused = await store.load_task(state.task_id)
    assert paused is not None
    assert paused.status is TaskStatus.AWAITING_CONFIRMATION

    await orch.revise(state.task_id, "redo the plan")
    result = await store.load_task(state.task_id)
    assert result is not None
    assert result.status is TaskStatus.REJECTED
    assert result.error == "I don't have a send_email tool"


async def test_stall_passes_prior_reasoning_to_next_attempt() -> None:
    # First attempt: long non-JSON output (over planner budget * multiplier).
    # Second attempt: a valid plan.
    # The capturing provider lets us confirm the stall content was forwarded.
    stall_output = "Let me think about this... " + ("x" * 600)
    orch, store, provider = _build_capturing(
        [stall_output, _PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT],
        settings=Settings(
            max_replan_attempts=1,
            budgets=NodeBudgets(planner=100),
            planner_stall_char_multiplier=4,
        ),
    )
    state = await _seed_task(store)

    await orch.run(state.task_id)
    result = await store.load_task(state.task_id)

    assert result is not None
    assert result.status is TaskStatus.COMPLETED
    assert len(result.planning_attempts) == 2
    assert result.planning_attempts[0].error_type == "stall"
    assert result.planning_attempts[0].error_detail == stall_output
    # The second planning call should contain the stalled output.
    assert len(provider.user_messages) >= 2
    assert "<previous_attempt>" in provider.user_messages[1]
    assert stall_output[:50] in provider.user_messages[1]


async def test_short_invalid_json_does_not_carry_prior_reasoning() -> None:
    # A short invalid-JSON response must NOT be treated as a stall.
    short_bad = "nope"  # well under 500 chars
    orch, store, provider = _build_capturing(
        [short_bad, _PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT],
        settings=Settings(max_replan_attempts=1),
    )
    state = await _seed_task(store)

    await orch.run(state.task_id)
    result = await store.load_task(state.task_id)

    assert result is not None
    assert result.status is TaskStatus.COMPLETED
    assert result.planning_attempts[0].error_type == "json"
    # The second planning call must NOT contain stalled output.
    assert len(provider.user_messages) >= 2
    assert "<previous_attempt>" not in provider.user_messages[1]


async def test_validation_error_passes_feedback_to_next_attempt() -> None:
    invalid_plan = json.dumps(
        {
            "nodes": [
                {
                    "id": 1,
                    "type": "result",
                    "description": "bad dependency",
                    "context_needed": [99],
                }
            ]
        }
    )
    orch, store, provider = _build_capturing(
        [invalid_plan, _PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT],
        settings=Settings(max_replan_attempts=1),
    )
    state = await _seed_task(store)

    await orch.run(state.task_id)
    result = await store.load_task(state.task_id)

    assert result is not None
    assert result.status is TaskStatus.COMPLETED
    assert result.planning_attempts[0].error_type == "validation"
    assert len(provider.user_messages) >= 2
    assert "<validation_feedback>" in provider.user_messages[1]
    assert "context_needed=99" in provider.user_messages[1]
    assert "<previous_attempt>" not in provider.user_messages[1]


async def test_conditional_plan_without_decision_retries_with_feedback() -> None:
    collapsed_plan = json.dumps(
        {
            "nodes": [
                {
                    "id": 1,
                    "type": "result",
                    "description": "Report 123 because 246 is even",
                    "context_needed": [],
                }
            ]
        }
    )
    corrected_plan = json.dumps(
        {
            "nodes": [
                {
                    "id": 1,
                    "type": "decision",
                    "description": "Decide whether 246 is even",
                    "condition": "Is 246 even?",
                    "branches": {"yes": [2], "no": [3]},
                    "default_branch": "yes",
                    "context_needed": [],
                },
                {
                    "id": 2,
                    "type": "result",
                    "description": "Report half of 246 as 123",
                    "context_needed": [1],
                },
                {
                    "id": 3,
                    "type": "result",
                    "description": "Report the next even number after 246",
                    "context_needed": [1],
                },
            ]
        }
    )
    orch, store, provider = _build_capturing(
        [
            collapsed_plan,
            corrected_plan,
            json.dumps({"branch": "yes", "confidence": 1.0}),
            json.dumps({"output": "123", "confidence": 1.0}),
        ],
        settings=Settings(max_replan_attempts=1),
    )
    state = await _seed_task(
        store,
        "Is 246 even or odd? If it is even, report half of it. "
        "If it is odd, report the next even number after it.",
    )

    await orch.run(state.task_id)
    result = await store.load_task(state.task_id)

    assert result is not None
    assert result.status is TaskStatus.COMPLETED
    assert result.final_output == "123"
    assert len(result.planning_attempts) == 2
    assert result.planning_attempts[0].error_type == "validation"
    assert "no decision node" in (result.planning_attempts[0].error or "")
    assert "<validation_feedback>" in provider.user_messages[1]
    assert "explicit decision node" in provider.user_messages[1]


async def test_exactly_stall_threshold_does_not_trigger_passthrough() -> None:
    # Exactly budget * multiplier must NOT trigger passthrough.
    at_threshold = "x" * 400
    orch, store, provider = _build_capturing(
        [at_threshold, _PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT],
        settings=Settings(
            max_replan_attempts=1,
            budgets=NodeBudgets(planner=100),
            planner_stall_char_multiplier=4,
        ),
    )
    state = await _seed_task(store)

    await orch.run(state.task_id)
    result = await store.load_task(state.task_id)

    assert result is not None
    assert result.status is TaskStatus.COMPLETED
    assert len(provider.user_messages) >= 2
    assert "<previous_attempt>" not in provider.user_messages[1]


class _BlockingProvider:
    """Yields the plan, then blocks on every later call until released."""

    def __init__(self, plan_payload: str, gate: asyncio.Event) -> None:
        self._plan = plan_payload
        self._gate = gate
        self.calls = 0

    @property
    def name(self) -> str:
        return "blocking"

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
        self.calls += 1
        if self.calls == 1:
            yield Chunk(text=self._plan, tokens_used=1)
            return
        await self._gate.wait()  # block mid-execution until released (never, here)
        yield Chunk(text="{}", tokens_used=0)


async def test_cancellation_marks_task_cancelled_and_emits_event() -> None:
    gate = asyncio.Event()  # never set: execution blocks at the first node LLM call
    provider = _BlockingProvider(_PLAN_PAYLOAD, gate)
    settings = Settings()
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([provider])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator, settings=settings)
    bus = EventBus()
    executor = Executor(router=router, harness=harness, settings=settings, event_bus=bus)
    store = InMemoryStateStore()
    orch = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
        event_bus=bus,
    )

    seen: list[str] = []

    async def collect() -> None:
        async with bus.subscribe() as sub:
            async for event in sub:
                seen.append(event.type)
                if event.type == "task_cancelled":
                    return

    collector = asyncio.create_task(collect())
    state = await _seed_task(store)
    run_task = asyncio.create_task(orch.run(state.task_id))

    # Let planning finish and execution reach the blocking node call.
    for _ in range(200):
        await asyncio.sleep(0)
        if provider.calls >= 2:
            break

    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(collector, timeout=1)

    stored = await store.load_task(state.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.CANCELLED
    assert stored.error == "cancelled"
    assert "task_cancelled" in seen


# Three-node plan: tool → synthesis → result.  The synthesis node (id 2)
# should see "Your output will be consumed by … Node 3" in its prompt because
# the result node (id 3) declares context_needed=[2].
_PLAN_THREE_NODES = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "tool",
                "description": "get today",
                "tool": "calendar_get",
                "args_template": {"date": "2026-05-27"},
                "context_needed": [],
            },
            {
                "id": 2,
                "type": "synthesis",
                "description": "summarise events",
                "context_needed": [1],
            },
            {
                "id": 3,
                "type": "result",
                "description": "final answer",
                "context_needed": [2],
            },
        ]
    }
)
_DONE_RESULT_OUT = json.dumps({"output": "done.", "confidence": 1.0})


async def test_dependent_nodes_section_in_synthesis_prompt() -> None:
    """synthesis node (id 2) should be told that result node (id 3) consumes it."""
    # Scripted responses in call order:
    # 0: planner → plan JSON
    # 1: tool shape-check → OK
    # 2: synthesis → output
    # 3: result → output
    orch, store, provider = _build_capturing(
        [_PLAN_THREE_NODES, _SHAPE_OK, _SYNTH_OUT, _DONE_RESULT_OUT],
    )
    state = await _seed_task(store)
    await orch.run(state.task_id)

    stored = await store.load_task(state.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED

    # The synthesis node is the 3rd LLM call (index 2).
    synthesis_msg = provider.user_messages[2]
    assert "Your output will be consumed by" in synthesis_msg
    assert "Node 3" in synthesis_msg
    assert "result" in synthesis_msg

    # The result node (index 3) is terminal — no dependents, no section.
    result_msg = provider.user_messages[3]
    assert "consumed by" not in result_msg
