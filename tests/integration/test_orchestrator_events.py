"""Integration tests for events emitted across a full orchestrated run.

A real Planner / Executor / Orchestrator run against a scripted provider,
with a shared EventBus wired into both the orchestrator and the executor.
A subscriber collects the stream and we assert the lifecycle sequence.
"""

from __future__ import annotations

import asyncio
import json

from glassrail.config import Settings
from glassrail.core import ExecutionState, new_task_id
from glassrail.events import Event, EventBus, PlanFailed, Subscription, TaskCompleted
from glassrail.executor import Executor, Orchestrator
from glassrail.harness import ToolHarness, register_builtins
from glassrail.planner import Planner
from glassrail.providers import TierRouter
from glassrail.state import InMemoryStateStore
from glassrail.validator import PlanValidator
from tests.conftest import make_scripted


def _build(
    responses: list[str],
    *,
    settings: Settings | None = None,
) -> tuple[Orchestrator, InMemoryStateStore, EventBus]:
    settings = settings or Settings()
    bus = EventBus()
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([make_scripted(responses)])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator, settings=settings)
    executor = Executor(router=router, harness=harness, settings=settings, event_bus=bus)
    store = InMemoryStateStore()
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
        event_bus=bus,
    )
    return orchestrator, store, bus


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


async def _seed_task(store: InMemoryStateStore) -> ExecutionState:
    state = ExecutionState(task_id=new_task_id(), user_request="what do I have today?")
    await store.save_task(state)
    return state


async def _drain(sub: Subscription) -> list[Event]:
    """Pull every already-delivered event; stop when the queue runs dry.

    The in-process bus delivers synchronously, so once the awaited run has
    returned, every event is already sitting in the subscriber's queue.
    """
    out: list[Event] = []
    while True:
        try:
            out.append(await asyncio.wait_for(anext(sub), 0.1))
        except TimeoutError:
            return out


async def test_full_run_emits_lifecycle_sequence() -> None:
    orch, store, bus = _build([_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT])
    state = await _seed_task(store)

    async with bus.subscribe() as sub:
        await orch.run(state.task_id)
        events = await _drain(sub)

    # Filter streaming chunks — this test is about lifecycle boundaries.
    types = [e.type for e in events if e.type != "node_output_chunk"]
    assert types == [
        "planning_started",
        "plan_ready",
        "node_started",
        "node_finished",
        "node_started",
        "node_finished",
        "node_started",
        "node_finished",
        "task_completed",
    ]

    terminal = events[-1]
    assert isinstance(terminal, TaskCompleted)
    assert terminal.final_output == "nothing scheduled."
    assert all(e.task_id == state.task_id for e in events)


async def test_failed_planning_emits_plan_failed_and_no_completion() -> None:
    orch, store, bus = _build(["not json", "still not json"])
    state = await _seed_task(store)

    async with bus.subscribe() as sub:
        await orch.run(state.task_id)
        events = await _drain(sub)

    types = [e.type for e in events]
    assert types == ["planning_started", "plan_failed"]
    failed = events[-1]
    assert isinstance(failed, PlanFailed)
    assert failed.attempts != []


async def test_confirm_gate_emits_awaiting_then_completes_on_resume() -> None:
    orch, store, bus = _build(
        [_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT, _RESULT_OUT],
        settings=Settings(confirm_plans=True),
    )
    state = await _seed_task(store)

    async with bus.subscribe() as sub:
        await orch.run(state.task_id)
        gated = await _drain(sub)
    assert [e.type for e in gated] == [
        "planning_started",
        "plan_ready",
        "awaiting_confirmation",
    ]

    async with bus.subscribe() as sub:
        await orch.resume(state.task_id)
        resumed = await _drain(sub)
    assert [e.type for e in resumed if e.type != "node_output_chunk"] == [
        "node_started",
        "node_finished",
        "node_started",
        "node_finished",
        "node_started",
        "node_finished",
        "task_completed",
    ]
