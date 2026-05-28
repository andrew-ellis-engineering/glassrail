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
from typing import cast

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
# fmt: on


class Executor:
    """Executes a validated plan and mutates the surrounding ExecutionState."""

    def __init__(
        self,
        *,
        router: TierRouter,
        harness: ToolHarness,
        settings: Settings,
    ) -> None:
        self._router = router
        self._harness = harness
        self._settings = settings

    async def execute(self, state: ExecutionState) -> ExecutionState:
        plan = state.plan
        if plan is None:
            raise RuntimeError("Cannot execute: state has no plan")

        skipped: set[int] = set()
        node_map = {n.id: n for n in plan.nodes}

        state.status = TaskStatus.EXECUTING
        state.touch()

        for node_id in plan.sorted_node_ids:
            node = node_map[node_id]

            if node_id in skipped:
                state.results[node_id] = NodeResult(node_id=node_id, status=NodeStatus.SKIPPED)
                state.skipped_nodes.append(node_id)
                log.info("Node %d skipped (excluded branch)", node_id)
                continue

            tier = self._select_tier(node)
            t0 = time.monotonic()

            if node.type is NodeType.TOOL:
                result = await self._execute_tool(node, state, tier)
            elif node.type is NodeType.DECISION:
                result = await self._execute_decision(node, state, tier)
                self._record_branch_decision(state, node, result, skipped)
            elif node.type is NodeType.SYNTHESIS:
                result = await self._execute_synthesis(node, state, tier)
            elif node.type is NodeType.THINK:
                result = await self._execute_think(node, state, tier)
            elif node.type is NodeType.SUMMARY:
                result = await self._execute_summary(node, state, tier)
            else:
                result = NodeResult(
                    node_id=node_id,
                    status=NodeStatus.FAILED,
                    error=f"Unknown node type: {node.type}",
                )

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

        state.final_output = self._extract_final_output(state, plan)
        state.status = TaskStatus.COMPLETED
        state.touch()
        return state

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

    async def _execute_think(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
    ) -> NodeResult:
        ctx = assemble_context(node, state.results)
        messages: list[Message] = [
            {"role": "system", "content": THINK_SYSTEM},
            {
                "role": "user",
                "content": f"Task: {node.description}\n\nContext from prior nodes:\n{ctx}",
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
            reasoning = data.get("reasoning", raw)
            confidence = float(data.get("confidence", 0.7))
        except Exception as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=reasoning,
            confidence=confidence,
            tokens_used=tokens,
        )

    async def _execute_summary(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
    ) -> NodeResult:
        ctx = assemble_context(node, state.results)
        messages: list[Message] = [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": f"Task: {node.description}\n\nContext to summarise:\n{ctx}",
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
            summary = data.get("summary", raw)
            confidence = float(data.get("confidence", 0.9))
        except Exception as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=summary,
            confidence=confidence,
            tokens_used=tokens,
        )

    async def _execute_synthesis(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
    ) -> NodeResult:
        ctx = assemble_context(node, state.results)
        messages: list[Message] = [
            {"role": "system", "content": SYNTHESIS_SYSTEM},
            {
                "role": "user",
                "content": f"Task: {node.description}\n\nContext from prior nodes:\n{ctx}",
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
            output = data.get("output", raw)
            confidence = float(data.get("confidence", 0.9))
        except Exception as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=output,
            confidence=confidence,
            tokens_used=tokens,
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
        """Last completed SYNTHESIS node's output is the final answer."""
        for node_id in reversed(plan.sorted_node_ids):
            node = next((n for n in plan.nodes if n.id == node_id), None)
            if node is None or node.type is not NodeType.SYNTHESIS:
                continue
            result = state.results.get(node_id)
            if result and result.status is NodeStatus.COMPLETED:
                return str(result.output)
        return None
