"""Plan executor — walks the DAG, dispatches per-node behaviour.

Every node sees a fresh context (see :mod:`dagagent.executor.context`).
Tier routing is deterministic: ``_select_tier`` decides which tier each
node runs at, the model never does.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import NamedTuple, cast

from dagagent.config import Settings
from dagagent.core import (
    BranchLogEntry,
    ExecutionState,
    Node,
    NodeResult,
    NodeStatus,
    NodeType,
    Plan,
    TaskStatus,
    ToolExecutionError,
)
from dagagent.events import (
    BranchDecided,
    Event,
    EventBus,
    NodeFinished,
    NodeStarted,
    TaskCompleted,
)
from dagagent.executor.context import assemble_context
from dagagent.harness import ToolHarness
from dagagent.providers import Message, TierRouter, collect

log = logging.getLogger(__name__)


# Prompts are kept verbatim; some lines exceed the 100-char lint limit.
# fmt: off
DECISION_SYSTEM = """\
You evaluate a binary condition based on provided context.
Respond ONLY with valid JSON: {"branch": "yes"|"no", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}
"""  # noqa: E501

SYNTHESIS_SYSTEM = """\
You are a synthesis engine. Use the provided context to produce a clear, concise response.
Always include a confidence score.
Respond ONLY with valid JSON: {"output": "<your response>", "confidence": <0.0-1.0>}
"""

UNEXPECTED_RESULT_SYSTEM = """\
You check whether a tool result matches its expected type.
Respond ONLY with valid JSON: {"matches_expectation": true|false, "issue": "<brief description or null>"}
"""  # noqa: E501

THINK_SYSTEM = """\
You are a reasoning engine. Work through the task step by step using the provided context.
Produce a concise chain of reasoning, then a confidence score for that reasoning.
Respond ONLY with valid JSON: {"reasoning": "<your step-by-step reasoning>", "confidence": <0.0-1.0>}
"""  # noqa: E501

SUMMARY_SYSTEM = """\
You are a summarisation engine. Condense the provided context into the shortest form that preserves the facts a downstream node needs. Drop boilerplate, redundancy, and formatting.
Respond ONLY with valid JSON: {"summary": "<condensed text>", "confidence": <0.0-1.0>}
"""  # noqa: E501

RESULT_SYSTEM = """\
You produce the final user-facing answer for a task. Use the provided context to compose a clean, direct response — no preamble, no meta-commentary, no scaffolding.
Respond ONLY with valid JSON: {"output": "<final answer>", "confidence": <0.0-1.0>}
"""  # noqa: E501
# fmt: on


class _LLMNodeSpec(NamedTuple):
    """Per-node-type knobs for the shared single-LLM-call node executor."""

    system: str
    context_label: str
    output_key: str
    default_confidence: float


# The synthesis / think / summary / result node types are all one LLM call
# that reads a single field out of a JSON response; only these few values
# differ. Keeping them in a table lets one method serve all four.
_LLM_NODE_SPECS: dict[NodeType, _LLMNodeSpec] = {
    NodeType.SYNTHESIS: _LLMNodeSpec(SYNTHESIS_SYSTEM, "Context from prior nodes:", "output", 0.9),
    NodeType.THINK: _LLMNodeSpec(THINK_SYSTEM, "Context from prior nodes:", "reasoning", 0.7),
    NodeType.SUMMARY: _LLMNodeSpec(SUMMARY_SYSTEM, "Context to summarise:", "summary", 0.9),
    NodeType.RESULT: _LLMNodeSpec(RESULT_SYSTEM, "Context from prior nodes:", "output", 0.9),
}


class Executor:
    """Executes a validated plan and mutates the surrounding ExecutionState."""

    def __init__(
        self,
        *,
        router: TierRouter,
        harness: ToolHarness,
        settings: Settings,
        event_bus: EventBus | None = None,
    ) -> None:
        self._router = router
        self._harness = harness
        self._settings = settings
        self._bus = event_bus

    async def execute(self, state: ExecutionState) -> ExecutionState:
        """Execute the plan, emitting lifecycle events to the bus."""
        return await self._run(state, emit=True)

    async def _run(self, state: ExecutionState, *, emit: bool) -> ExecutionState:
        plan = state.plan
        if plan is None:
            raise RuntimeError("Cannot execute: state has no plan")

        # Nested subplan runs reuse this method with emit=False so their
        # internal nodes (whose ids may collide with the parent's) don't
        # leak onto the parent task's event stream.
        bus = self._bus if emit else None
        skipped: set[int] = set()
        node_map = {n.id: n for n in plan.nodes}

        state.status = TaskStatus.EXECUTING
        state.touch()

        for node_id in plan.sorted_node_ids:
            node = node_map[node_id]

            if node_id in skipped:
                result = NodeResult(node_id=node_id, status=NodeStatus.SKIPPED)
                state.results[node_id] = result
                state.skipped_nodes.append(node_id)
                log.info("Node %d skipped (excluded branch)", node_id)
                await self._emit(bus, self._finished_event(state, result))
                continue

            tier = self._select_tier(node)
            await self._emit(
                bus,
                NodeStarted(
                    task_id=state.task_id,
                    node_id=node_id,
                    node_type=node.type,
                    tier=tier,
                ),
            )
            t0 = time.monotonic()
            result = await self._dispatch_node(node, state, tier, skipped)
            result.execution_time_s = time.monotonic() - t0
            result.tier_used = tier

            if (
                result.status is NodeStatus.COMPLETED
                and result.confidence < self._settings.confidence_threshold
            ):
                result.flagged = True
                log.warning(
                    "Node %d flagged: confidence %.2f below threshold",
                    node_id,
                    result.confidence,
                )

            state.results[node_id] = result
            state.completed_nodes.append(node_id)
            state.touch()

            if node.type is NodeType.DECISION:
                await self._emit(
                    bus,
                    BranchDecided(
                        task_id=state.task_id,
                        node_id=node_id,
                        branch_taken=result.branch_taken,
                        confidence=result.confidence,
                    ),
                )
            await self._emit(bus, self._finished_event(state, result))

        state.final_output = self._extract_final_output(state, plan)
        state.status = TaskStatus.COMPLETED
        state.touch()
        await self._emit(bus, TaskCompleted(task_id=state.task_id, final_output=state.final_output))
        return state

    async def _dispatch_node(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
        skipped: set[int],
    ) -> NodeResult:
        if node.type is NodeType.TOOL:
            return await self._execute_tool(node, state, tier)
        if node.type is NodeType.DECISION:
            result = await self._execute_decision(node, state, tier)
            self._record_branch_decision(state, node, result, skipped)
            return result
        if node.type is NodeType.SUBPLAN:
            return await self._execute_subplan(node, state)
        spec = _LLM_NODE_SPECS.get(node.type)
        if spec is not None:
            return await self._execute_llm_node(node, state, tier, spec=spec)
        return NodeResult(
            node_id=node.id,
            status=NodeStatus.FAILED,
            error=f"Unknown node type: {node.type}",
        )

    @staticmethod
    async def _emit(bus: EventBus | None, event: Event) -> None:
        if bus is not None:
            await bus.publish(event)

    @staticmethod
    def _finished_event(state: ExecutionState, result: NodeResult) -> NodeFinished:
        return NodeFinished(
            task_id=state.task_id,
            node_id=result.node_id,
            status=result.status,
            confidence=result.confidence,
            flagged=result.flagged,
            tier_used=result.tier_used,
        )

    # ── Per-node executors ────────────────────────────────────────────────

    async def _execute_tool(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
    ) -> NodeResult:
        if node.tool is None:
            return NodeResult(
                node_id=node.id,
                status=NodeStatus.FAILED,
                error="TOOL node has no tool name",
            )

        ctx = assemble_context(node, state.results)
        args: dict[str, object] = dict(node.args_template or {})

        if ctx and not args:
            args = await self._extract_args(node, ctx, tier)

        try:
            raw_output = await self._harness.execute(node.tool, args)
        except ToolExecutionError as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        if raw_output is None or raw_output in ({}, []):
            log.info("Node %d: tool '%s' returned empty", node.id, node.tool)
            return NodeResult(node_id=node.id, status=NodeStatus.EMPTY, output=raw_output)

        ok, issue = await self._check_output_shape(node, raw_output)
        if not ok:
            log.warning("Node %d unexpected result shape: %s", node.id, issue)
            return NodeResult(
                node_id=node.id,
                status=NodeStatus.COMPLETED,
                output=raw_output,
                confidence=0.5,
                flagged=True,
                error=f"Unexpected shape: {issue}",
            )

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=raw_output,
            confidence=1.0,
        )

    async def _execute_decision(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
    ) -> NodeResult:
        ctx = assemble_context(node, state.results)
        messages: list[Message] = [
            {"role": "system", "content": DECISION_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Condition to evaluate: {node.condition}\n\nAvailable context:\n{ctx}"
                ),
            },
        ]

        tokens = 0
        try:
            raw, tokens = await collect(
                self._router.complete(
                    messages,
                    min_tier=tier,
                    json_mode=True,
                    max_tokens=256,
                )
            )
            data = json.loads(raw)
            branch = data.get("branch", node.default_branch or "yes")
            confidence = float(data.get("confidence", 0.5))
            if node.branches and branch not in node.branches:
                log.warning(
                    "Node %d returned unknown branch '%s', falling back to default '%s'",
                    node.id,
                    branch,
                    node.default_branch,
                )
                branch = node.default_branch or next(iter(node.branches))
        except Exception as exc:
            log.warning("Decision node %d failed (%s), using default branch", node.id, exc)
            branch = node.default_branch or (next(iter(node.branches)) if node.branches else "yes")
            confidence = 0.0

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            branch_taken=branch,
            confidence=confidence,
            tokens_used=tokens,
        )

    async def _execute_llm_node(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
        *,
        spec: _LLMNodeSpec,
    ) -> NodeResult:
        """Run a single-LLM-call node (synthesis / think / summary / result).

        These node types differ only in their system prompt, the label they
        give the upstream context, which JSON field carries their output, and
        their default confidence — all supplied by ``spec``.
        """
        ctx = assemble_context(node, state.results)
        messages: list[Message] = [
            {"role": "system", "content": spec.system},
            {
                "role": "user",
                "content": f"Task: {node.description}\n\n{spec.context_label}\n{ctx}",
            },
        ]
        try:
            raw, tokens = await collect(
                self._router.complete(
                    messages,
                    min_tier=tier,
                    json_mode=True,
                    max_tokens=self._settings.max_node_output_tokens,
                )
            )
            data = json.loads(raw)
            output = data.get(spec.output_key, raw)
            confidence = float(data.get("confidence", spec.default_confidence))
        except Exception as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=output,
            confidence=confidence,
            tokens_used=tokens,
        )

    async def _execute_subplan(
        self,
        node: Node,
        state: ExecutionState,
    ) -> NodeResult:
        """Run the nested plan in a fresh ExecutionState and bubble up its
        final_output as this node's output. The subplan does not see the
        parent's results — only the upstream outputs the parent passes in
        as its description's context."""
        if node.subplan is None:
            return NodeResult(
                node_id=node.id,
                status=NodeStatus.FAILED,
                error="SUBPLAN node has no nested plan",
            )

        ctx = assemble_context(node, state.results)
        sub_request = (
            f"{node.description}\n\nContext from parent plan:\n{ctx}" if ctx else node.description
        )
        sub_state = ExecutionState(
            task_id=state.task_id,
            user_request=sub_request,
            plan=node.subplan,
        )
        try:
            completed = await self._run(sub_state, emit=False)
        except Exception as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        if completed.final_output is None:
            return NodeResult(
                node_id=node.id,
                status=NodeStatus.EMPTY,
                error="Subplan produced no final output",
            )

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=completed.final_output,
            confidence=1.0,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _select_tier(self, node: Node) -> int:
        """Pick a tier for ``node``. Deterministic — the model never decides."""
        if node.forced_tier is not None:
            return node.forced_tier
        if node.type is NodeType.DECISION:
            return 0
        if node.type is NodeType.TOOL:
            return 0
        if node.type is NodeType.THINK:
            return 2
        if node.reasoning_required:
            return 2
        return 0

    async def _extract_args(
        self,
        node: Node,
        ctx: str,
        tier: int,
    ) -> dict[str, object]:
        schema = self._harness.schema_for(node.tool or "")
        prompt = (
            f"Extract the arguments for tool '{node.tool}' from the context below.\n"
            f"Tool schema: {json.dumps(schema)}\n\n"
            f"Context:\n{ctx}\n\n"
            "Respond ONLY with a JSON object of the arguments."
        )
        try:
            raw, _ = await collect(
                self._router.complete(
                    [{"role": "user", "content": prompt}],
                    min_tier=tier,
                    json_mode=True,
                    max_tokens=512,
                )
            )
            data = json.loads(raw)
            if isinstance(data, dict):
                return cast("dict[str, object]", data)
            return {}
        except Exception:
            return {}

    async def _check_output_shape(
        self,
        node: Node,
        output: object,
    ) -> tuple[bool, str | None]:
        """Lightweight gate: does the tool output look like what the node asked for?"""
        try:
            output_str = json.dumps(output)[:1000]
        except (TypeError, ValueError):
            output_str = str(output)[:1000]

        prompt = (
            f"Tool: {node.tool}\nDescription: {node.description}\nOutput received:\n{output_str}"
        )
        try:
            raw, _ = await collect(
                self._router.complete(
                    [
                        {"role": "system", "content": UNEXPECTED_RESULT_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    min_tier=0,
                    json_mode=True,
                    max_tokens=128,
                )
            )
            data = json.loads(raw)
            ok = bool(data.get("matches_expectation", True))
            issue = data.get("issue")
            return ok, issue if isinstance(issue, str) else None
        except Exception:
            # If the gate itself fails, don't block execution.
            return True, None

    def _record_branch_decision(
        self,
        state: ExecutionState,
        node: Node,
        result: NodeResult,
        skipped: set[int],
    ) -> None:
        if result.branch_taken and node.branches:
            for branch_name, branch_nodes in node.branches.items():
                if branch_name != result.branch_taken:
                    skipped.update(branch_nodes)
        state.branch_log.append(
            BranchLogEntry(
                node_id=node.id,
                condition=node.condition,
                branch_taken=result.branch_taken,
                confidence=result.confidence,
                timestamp=datetime.now(UTC),
            )
        )
        log.info(
            "Decision node %d: condition='%s' → branch='%s' (confidence=%.2f)",
            node.id,
            node.condition,
            result.branch_taken,
            result.confidence,
        )

    def _extract_final_output(self, state: ExecutionState, plan: Plan) -> str | None:
        """The last completed RESULT node's output is the final answer; if
        the plan declared no RESULT node, fall back to the last completed
        SYNTHESIS node so older plans keep working unchanged."""
        for target in (NodeType.RESULT, NodeType.SYNTHESIS):
            for node_id in reversed(plan.sorted_node_ids):
                node = next((n for n in plan.nodes if n.id == node_id), None)
                if node is None or node.type is not target:
                    continue
                result = state.results.get(node_id)
                if result and result.status is NodeStatus.COMPLETED:
                    return str(result.output)
        return None
