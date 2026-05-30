"""Integration tests for the Orchestrator.

End-to-end-ish: real Planner, real Executor, real PlanValidator, real
ToolHarness, real InMemoryStateStore, fake LLM provider scripted with
the responses the prompt would normally elicit.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

from dagagent.config import Settings
from dagagent.core import ExecutionState, TaskStatus, new_task_id
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
        ]
    }
)
_SHAPE_OK = json.dumps({"matches_expectation": True, "issue": None})
_SYNTH_OUT = json.dumps({"output": "nothing scheduled.", "confidence": 0.9})


async def _seed_task(
    store: InMemoryStateStore,
    request: str = "what do I have today?",
) -> ExecutionState:
    state = ExecutionState(task_id=new_task_id(), user_request=request)
    await store.save_task(state)
    return state


async def test_full_flow_plan_validate_execute() -> None:
    orch, store = _build([_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT])
    state = await _seed_task(store)

    await orch.run(state.task_id)
    stored = await store.load_task(state.task_id)

    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED
    assert stored.plan is not None
    assert len(stored.plan.nodes) == 2
    assert stored.final_output == "nothing scheduled."


async def test_confirm_gate_pauses_and_resume_finishes() -> None:
    orch, store = _build(
        [_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT],
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


async def test_failed_planning_marks_task_failed() -> None:
    orch, store = _build(["this isn't even close to JSON", "still not JSON"])
    state = await _seed_task(store)

    await orch.run(state.task_id)
    failed = await store.load_task(state.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error is not None
    assert "Planning failed" in failed.error


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
