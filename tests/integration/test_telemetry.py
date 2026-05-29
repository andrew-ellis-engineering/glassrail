"""Integration test: a run emits a task -> plan/node -> LLM span tree.

Wires the real planner/executor/validator/orchestrator around a scripted
provider, installs an in-memory span exporter, and asserts the exported spans
form the expected tree with the key GenAI / dagagent attributes.

Installing a tracer provider is process-global and set-once, so this is the
only test that asserts on spans; others tolerate the provider being present.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from dagagent.config import Settings
from dagagent.core import ExecutionState, TaskStatus, new_task_id
from dagagent.executor import Executor, Orchestrator
from dagagent.harness import ToolHarness, register_builtins
from dagagent.planner import Planner
from dagagent.providers import Chunk, Message, TierRouter
from dagagent.state import InMemoryStateStore
from dagagent.telemetry import (
    ATTR_GEN_AI_USAGE_TOTAL_TOKENS,
    ATTR_NODE_TYPE,
    ATTR_TASK_STATUS,
    ATTR_TIER,
    SPAN_LLM,
    SPAN_NODE,
    SPAN_PLAN,
    SPAN_TASK,
    configure_tracing,
)
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

    @property
    def model(self) -> str:
        return "scripted-model"

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        del messages, json_mode, max_tokens, timeout_s
        yield Chunk(text=self._responses.pop(0), tokens_used=1)


_PLAN = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "tool",
                "description": "get today",
                "tool": "calendar_get",
                "args_template": {"date": "2026-05-29"},
                "context_needed": [],
            },
            {"id": 2, "type": "result", "description": "answer", "context_needed": [1]},
        ]
    }
)
_SHAPE_OK = json.dumps({"matches_expectation": True, "issue": None})
_RESULT = json.dumps({"output": "nothing scheduled.", "confidence": 0.9})


def _build(responses: list[str]) -> tuple[Orchestrator, InMemoryStateStore]:
    settings = Settings()
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_Scripted(responses)])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator)
    executor = Executor(router=router, harness=harness, settings=settings)
    store = InMemoryStateStore()
    orchestrator = Orchestrator(
        planner=planner, executor=executor, state_store=store, settings=settings
    )
    return orchestrator, store


def _ids(spans: _Sequence[ReadableSpan], name: str) -> set[int]:
    return {s.context.span_id for s in spans if s.name == name and s.context is not None}


def _attr(span: ReadableSpan, key: str) -> object:
    return (span.attributes or {}).get(key)


async def test_run_emits_task_plan_node_llm_span_tree() -> None:
    exporter = InMemorySpanExporter()
    assert configure_tracing(Settings(), span_exporter=exporter) is True
    exporter.clear()  # drop anything other tests may have recorded first

    orch, store = _build([_PLAN, _SHAPE_OK, _RESULT])
    state = ExecutionState(task_id=new_task_id(), user_request="what do I have today?")
    await store.save_task(state)
    await orch.run(state.task_id)

    spans = exporter.get_finished_spans()
    task_ids = _ids(spans, SPAN_TASK)
    plan_ids = _ids(spans, SPAN_PLAN)
    node_ids = _ids(spans, SPAN_NODE)
    llm_spans = [s for s in spans if s.name == SPAN_LLM]

    # One task, one plan, two nodes (tool + result), three LLM calls
    # (planner, tool shape-check, result synthesis).
    assert len(task_ids) == 1
    assert len(plan_ids) == 1
    assert len(node_ids) == 2
    assert len(llm_spans) == 3

    (task_id,) = tuple(task_ids)
    (plan_id,) = tuple(plan_ids)

    # The plan and node spans hang directly off the task span.
    plan_span = next(s for s in spans if s.name == SPAN_PLAN)
    assert plan_span.parent is not None and plan_span.parent.span_id == task_id
    for node in (s for s in spans if s.name == SPAN_NODE):
        assert node.parent is not None and node.parent.span_id == task_id

    # Every LLM span nests under the plan span or one of the node spans.
    parents = plan_ids | node_ids
    for llm in llm_spans:
        assert llm.parent is not None and llm.parent.span_id in parents
    # Exactly one LLM call (the planner) sits under the plan span.
    assert sum(1 for s in llm_spans if s.parent and s.parent.span_id == plan_id) == 1

    # Attributes: node type recorded, LLM tokens + tier recorded, task completed.
    node_types = {_attr(s, ATTR_NODE_TYPE) for s in spans if s.name == SPAN_NODE}
    assert node_types == {"tool", "result"}
    assert any(_attr(s, ATTR_GEN_AI_USAGE_TOTAL_TOKENS) == 1 for s in llm_spans)
    assert all(_attr(s, ATTR_TIER) == 0 for s in llm_spans)
    task_span = next(s for s in spans if s.name == SPAN_TASK)
    assert _attr(task_span, ATTR_TASK_STATUS) == TaskStatus.COMPLETED.value


async def test_run_completes_when_tracing_unconfigured() -> None:
    # No configure_tracing here: instrumentation must be a transparent no-op.
    orch, store = _build([_PLAN, _SHAPE_OK, _RESULT])
    state = ExecutionState(task_id=new_task_id(), user_request="what do I have today?")
    await store.save_task(state)
    await orch.run(state.task_id)

    done = await store.load_task(state.task_id)
    assert done is not None
    assert done.status is TaskStatus.COMPLETED
    assert done.final_output == "nothing scheduled."
