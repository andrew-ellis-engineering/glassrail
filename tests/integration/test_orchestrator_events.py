"""Integration tests for events emitted across a full orchestrated run.

A real Planner / Executor / Orchestrator run against a scripted provider,
with a shared EventBus wired into both the orchestrator and the executor.
A subscriber collects the stream and we assert the lifecycle sequence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

from dagagent.config import Settings
from dagagent.core import ExecutionState, new_task_id
from dagagent.events import Event, EventBus, Subscription, TaskCompleted
from dagagent.executor import Executor, Orchestrator
from dagagent.harness import ToolHarness, register_builtins
from dagagent.planner import Planner
from dagagent.providers import Chunk, Message, TierRouter
from dagagent.state import InMemoryStateStore
from dagagent.validator import PlanValidator


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


def _build(
    responses: list[str],
    *,
    settings: Settings | None = None,
) -> tuple[Orchestrator, InMemoryStateStore, EventBus]:
    settings = settings or Settings()
    bus = EventBus()
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_Scripted(responses)])
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
        ]
    }
)
_SHAPE_OK = json.dumps({"matches_expectation": True, "issue": None})
_SYNTH_OUT = json.dumps({"output": "nothing scheduled.", "confidence": 0.9})


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
    orch, store, bus = _build([_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT])
    state = await _seed_task(store)

    async with bus.subscribe() as sub:
        await orch.run(state.task_id)
        events = await _drain(sub)

    types = [e.type for e in events]
    assert types == [
        "planning_started",
        "plan_ready",
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


async def test_confirm_gate_emits_awaiting_then_completes_on_resume() -> None:
    orch, store, bus = _build(
        [_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT],
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
    assert [e.type for e in resumed] == [
        "node_started",
        "node_finished",
        "node_started",
        "node_finished",
        "task_completed",
    ]
