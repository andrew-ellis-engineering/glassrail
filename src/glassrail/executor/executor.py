"""Plan executor — walks the DAG, dispatches per-node behaviour.

Every node sees a fresh context (see :mod:`glassrail.executor.context`).
Tier routing is deterministic: ``_select_tier`` decides which tier each
node runs at, the model never does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar, NamedTuple, cast

from opentelemetry.trace import Status, StatusCode

from glassrail.config import Settings, ToolApprovalMode, ToolApprovalPolicy
from glassrail.core import (
    BranchLogEntry,
    ExecutionState,
    Node,
    NodeResult,
    NodeStatus,
    NodeType,
    Plan,
    SummaryFormat,
    TaskStatus,
    ToolExecutionError,
)
from glassrail.events import (
    BranchDecided,
    Event,
    EventBus,
    NodeFinished,
    NodeOutputChunk,
    NodeStarted,
    TaskCompleted,
)
from glassrail.executor.context import assemble_context
from glassrail.executor.tool_approval import ToolApprovalBroker
from glassrail.harness import ToolHarness
from glassrail.providers import (
    Chunk,
    Message,
    ProviderUnavailableError,
    TierRouter,
    collect,
    strip_model_output,
)
from glassrail.telemetry import (
    ATTR_NODE_CONFIDENCE,
    ATTR_NODE_ID,
    ATTR_NODE_RETRIES,
    ATTR_NODE_STATUS,
    ATTR_NODE_TYPE,
    ATTR_TIER,
    SPAN_NODE,
    get_tracer,
)

log = logging.getLogger(__name__)


class _EmptyLLMOutputError(Exception):
    """Retryable model-node failure: the provider returned only blank text."""

    def __init__(self, *, tokens_used: int) -> None:
        super().__init__("empty model output")
        self.tokens_used = tokens_used


def _clamp_confidence(value: float, default: float, node_id: int) -> float:
    """Clamp confidence to [0, 1] and replace NaN/Inf with ``default``."""
    if math.isnan(value) or math.isinf(value):
        log.warning("Node %d: confidence is %s; substituting default %.2f", node_id, value, default)
        return default
    return max(0.0, min(1.0, value))


class _LLMNodeSpec(NamedTuple):
    """Per-node-type knobs for the shared single-LLM-call node executor.

    The system prompt is *not* here — it is read from ``settings.prompts`` so it
    can be tuned without code changes. This table holds only the parsing knobs
    that must stay in lockstep with that prompt's JSON shape.
    """

    context_label: str
    output_key: str
    default_confidence: float


# The synthesis / think / summary / result node types are all one LLM call
# that reads a single field out of a JSON response; only these few values
# differ. Keeping them in a table lets one method serve all four.
_LLM_NODE_SPECS: dict[NodeType, _LLMNodeSpec] = {
    NodeType.SYNTHESIS: _LLMNodeSpec("Context from prior nodes:", "output", 0.9),
    NodeType.THINK: _LLMNodeSpec("Context from prior nodes:", "reasoning", 0.7),
    NodeType.SUMMARY: _LLMNodeSpec("Context to summarise:", "summary", 0.9),
    NodeType.RESULT: _LLMNodeSpec("Context from prior nodes:", "output", 0.9),
}

# Node types whose text output is streamed live as NodeOutputChunk events.
# Mirrors the ACP adapter's _MESSAGE_NODE_TYPES (think / synthesis / summary);
# the result node is excluded because its output surfaces via TaskCompleted.
_STREAMING_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.THINK, NodeType.SYNTHESIS, NodeType.SUMMARY}
)


class JsonFieldStreamer:
    """Incrementally extract a named JSON string field from a streaming text.

    The provider streams raw JSON like ``{"reasoning": "...text...",
    "confidence": 0.7}`` one small chunk at a time. This class buffers the
    incoming bytes and emits the content of the named field as soon as each
    character is available, without waiting for the full response.

    Usage::

        streamer = JsonFieldStreamer("reasoning")
        async for chunk in provider.complete(...):
            new_text = streamer.feed(chunk.text)
            if new_text:
                # emit or display new_text
        # After the loop, check streamer.done to see if the field was found.
    """

    # Dict of simple single-char JSON escape sequences to their decoded values.
    _ESCAPES: ClassVar[dict[str, str]] = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        '"': '"',
        "\\": "\\",
        "/": "/",
    }

    def __init__(self, field: str) -> None:
        # Both "key": " (with space) and "key":" (without) are valid JSON.
        self._marker = f'"{field}": "'
        self._alt_marker = f'"{field}":"'
        self._buf = ""
        self._pos = 0
        self._found = False
        self._done = False
        self._escape = False
        # When not None, we are accumulating the 4 hex digits of a \uXXXX escape.
        self._unicode_buf: str | None = None

    def feed(self, chunk: str) -> str:
        """Feed the next raw chunk; return any new field content to emit.

        The streamed text matches the decoded content of the JSON field. Escape
        sequences ``\\n``, ``\\t``, ``\\r``, ``\\"``, ``\\\\``, and ``\\uXXXX``
        (BMP only) are decoded; surrogate pairs and unknown sequences pass
        through as-is.
        """
        if self._done or not chunk:
            return ""

        self._buf += chunk

        if not self._found and not self._scan_for_marker():
            return ""

        result: list[str] = []
        while self._pos < len(self._buf):
            c = self._buf[self._pos]
            self._pos += 1
            if self._unicode_buf is not None:
                result.append(self._feed_unicode_digit(c))
            elif self._escape:
                if c == "u":
                    self._unicode_buf = ""
                else:
                    result.append(self._ESCAPES.get(c, c))
                self._escape = False
            elif c == "\\":
                self._escape = True
            elif c == '"':
                self._done = True
                break
            else:
                result.append(c)

        return "".join(result)

    def _scan_for_marker(self) -> bool:
        """Scan the accumulated buffer for the field marker.

        Returns True and advances ``_pos`` to the start of the field's value
        if found. Otherwise trims the buffer tail to avoid unbounded growth
        (keeping enough for the marker to straddle a chunk boundary) and
        returns False.
        """
        for marker in (self._marker, self._alt_marker):
            idx = self._buf.find(marker)
            if idx != -1:
                self._pos = idx + len(marker)
                self._found = True
                return True
        keep = len(self._marker) + 5
        if len(self._buf) > keep:
            self._buf = self._buf[-keep:]
            self._pos = 0
        return False

    def _feed_unicode_digit(self, c: str) -> str:
        """Accumulate one hex digit for a ``\\uXXXX`` escape; return decoded char when complete."""
        assert self._unicode_buf is not None
        self._unicode_buf += c
        if len(self._unicode_buf) < 4:
            return ""
        try:
            ch = chr(int(self._unicode_buf, 16))
        except ValueError:
            ch = "\\u" + self._unicode_buf
        self._unicode_buf = None
        return ch

    @property
    def done(self) -> bool:
        """True once the closing quote of the field value has been seen."""
        return self._done


class Executor:
    """Executes a validated plan and mutates the surrounding ExecutionState."""

    def __init__(
        self,
        *,
        router: TierRouter,
        harness: ToolHarness,
        settings: Settings,
        event_bus: EventBus | None = None,
        tool_approval: ToolApprovalBroker | None = None,
    ) -> None:
        self._router = router
        self._harness = harness
        self._settings = settings
        self._bus = event_bus
        self._tool_approval = tool_approval

    async def execute(self, state: ExecutionState) -> ExecutionState:
        """Execute the plan, emitting lifecycle events to the bus."""
        return await self._run(state, emit=True)

    async def _run(
        self,
        state: ExecutionState,
        *,
        emit: bool,
        semaphore: asyncio.Semaphore | None = None,
        path_prefix: str = "",
        emit_task_completed: bool = True,
    ) -> ExecutionState:
        plan = state.plan
        if plan is None:
            raise RuntimeError("Cannot execute: state has no plan")

        # Nested subplan runs reuse this method with emit=False so their
        # internal nodes (whose ids may collide with the parent's) don't
        # leak onto the parent task's event stream.
        bus = self._bus if emit else None
        node_map = {n.id: n for n in plan.nodes}
        deps = self._dependency_map(plan)
        max_concurrent = max(1, self._settings.max_concurrent_nodes)
        semaphore = semaphore or asyncio.Semaphore(max_concurrent)
        pending: set[int] = set(plan.sorted_node_ids)
        in_flight: dict[asyncio.Task[None], int] = {}

        state.status = TaskStatus.EXECUTING
        state.touch()

        while pending or in_flight:
            dispatched = False
            for node_id in plan.sorted_node_ids:
                if node_id not in pending:
                    continue
                node = node_map[node_id]
                if node_id in state.results:
                    pending.remove(node_id)
                    continue
                if not deps[node_id].issubset(state.results):
                    continue

                pending.remove(node_id)
                if self._only_uses_skipped_content(
                    node, state, plan, missing_counts_as_skipped=False
                ):
                    await self._record_skipped_node(
                        state,
                        node_id=node_id,
                        bus=bus,
                        reason="all content dependencies were skipped",
                        node_path=self._node_path(path_prefix, node_id),
                    )
                    dispatched = True
                    continue

                if len(in_flight) >= max_concurrent:
                    pending.add(node_id)
                    break

                task = asyncio.create_task(
                    self._run_node(
                        node,
                        state,
                        plan=plan,
                        node_map=node_map,
                        semaphore=semaphore,
                        bus=bus,
                        path_prefix=path_prefix,
                    )
                )
                in_flight[task] = node_id
                dispatched = True

            if not in_flight:
                if pending and not dispatched:
                    blocked = {
                        node_id: sorted(deps[node_id] - state.results.keys())
                        for node_id in sorted(pending)
                    }
                    raise RuntimeError(f"Cannot execute plan: no ready nodes remain: {blocked}")
                continue

            if dispatched and len(in_flight) < max_concurrent:
                continue

            done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                in_flight.pop(task)
                await task

        state.final_output = self._extract_final_output(state, plan)
        state.status = TaskStatus.COMPLETED
        state.touch()
        await self._emit(
            bus if emit_task_completed else None,
            TaskCompleted(task_id=state.task_id, final_output=state.final_output),
        )
        return state

    async def _run_node(
        self,
        node: Node,
        state: ExecutionState,
        *,
        plan: Plan,
        node_map: dict[int, Node],
        semaphore: asyncio.Semaphore,
        bus: EventBus | None,
        path_prefix: str,
    ) -> None:
        if node.type is NodeType.SUBPLAN:
            await self._execute_and_record_node(
                node,
                state,
                plan=plan,
                node_map=node_map,
                semaphore=semaphore,
                bus=bus,
                path_prefix=path_prefix,
            )
            return

        async with semaphore:
            await self._execute_and_record_node(
                node,
                state,
                plan=plan,
                node_map=node_map,
                semaphore=semaphore,
                bus=bus,
                path_prefix=path_prefix,
            )

    async def _execute_and_record_node(
        self,
        node: Node,
        state: ExecutionState,
        *,
        plan: Plan,
        node_map: dict[int, Node],
        semaphore: asyncio.Semaphore,
        bus: EventBus | None,
        path_prefix: str,
    ) -> None:
        node_id = node.id
        node_path = self._node_path(path_prefix, node_id)
        tier = self._select_tier(node)
        with get_tracer().start_as_current_span(SPAN_NODE) as span:
            span.set_attribute(ATTR_NODE_ID, node_id)
            span.set_attribute(ATTR_NODE_TYPE, node.type.value)
            span.set_attribute(ATTR_TIER, tier)

            await self._emit(
                bus,
                NodeStarted(
                    task_id=state.task_id,
                    node_id=node_id,
                    node_type=node.type,
                    tier=tier,
                    node_path=node_path,
                ),
            )
            t0 = time.monotonic()
            result = await self._dispatch_node(
                node,
                state,
                tier,
                semaphore=semaphore,
                bus=bus,
                path_prefix=path_prefix,
                node_path=node_path,
            )
            result.execution_time_s = time.monotonic() - t0
            if result.tier_used is None:
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

            span.set_attribute(ATTR_NODE_STATUS, result.status.value)
            span.set_attribute(ATTR_NODE_CONFIDENCE, result.confidence)
            span.set_attribute(ATTR_NODE_RETRIES, result.retries)
            if result.status is NodeStatus.FAILED:
                span.set_status(Status(StatusCode.ERROR, result.error or "node failed"))

            state.results[node_id] = result
            state.completed_nodes.append(node_id)
            state.touch()

            if node.type is NodeType.DECISION:
                skipped = self._record_branch_decision(state, node, result)
                await self._emit(
                    bus,
                    BranchDecided(
                        task_id=state.task_id,
                        node_id=node_id,
                        branch_taken=result.branch_taken,
                        confidence=result.confidence,
                        node_path=node_path,
                    ),
                )
            else:
                skipped: set[int] = set()

            await self._emit(bus, self._finished_event(state, result, node_path=node_path))

        for skipped_id in sorted(skipped):
            if skipped_id in node_map and skipped_id not in state.results:
                await self._record_skipped_node(
                    state,
                    node_id=skipped_id,
                    bus=bus,
                    reason="excluded branch",
                    node_path=self._node_path(path_prefix, skipped_id),
                )

    async def _dispatch_node(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
        *,
        semaphore: asyncio.Semaphore,
        path_prefix: str,
        node_path: str | None,
        bus: EventBus | None = None,
    ) -> NodeResult:
        if node.type is NodeType.TOOL:
            return await self._execute_tool(node, state, tier)
        if node.type is NodeType.DECISION:
            return await self._execute_decision(node, state, tier)
        if node.type is NodeType.SUBPLAN:
            return await self._execute_subplan(
                node,
                state,
                semaphore=semaphore,
                emit_nested=bus is not None,
                path_prefix=path_prefix,
            )
        spec = _LLM_NODE_SPECS.get(node.type)
        if spec is not None:
            return await self._execute_llm_node(
                node, state, tier, spec=spec, bus=bus, node_path=node_path
            )
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
    def _finished_event(
        state: ExecutionState,
        result: NodeResult,
        *,
        node_path: str | None = None,
    ) -> NodeFinished:
        return NodeFinished(
            task_id=state.task_id,
            node_id=result.node_id,
            status=result.status,
            confidence=result.confidence,
            flagged=result.flagged,
            tier_used=result.tier_used,
            error=result.error,
            node_path=node_path,
        )

    @staticmethod
    async def _record_skipped_node(
        state: ExecutionState,
        *,
        node_id: int,
        bus: EventBus | None,
        reason: str,
        node_path: str | None = None,
    ) -> None:
        result = NodeResult(node_id=node_id, status=NodeStatus.SKIPPED)
        state.results[node_id] = result
        if node_id not in state.skipped_nodes:
            state.skipped_nodes.append(node_id)
        state.touch()
        log.info("Node %d skipped (%s)", node_id, reason)
        await Executor._emit(bus, Executor._finished_event(state, result, node_path=node_path))

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

        ctx = assemble_context(
            node,
            state.results,
            dependent_nodes=self._direct_dependents(node, state) or None,
        )
        args: dict[str, object] = dict(node.args_template or {})

        if ctx and not args:
            args = await self._extract_args(node, ctx, tier)

        approved = await self._approve_tool_call(node=node, state=state, args=args)
        if not approved:
            return NodeResult(
                node_id=node.id,
                status=NodeStatus.FAILED,
                error=f"user_denied: tool '{node.tool}' was not approved",
            )

        try:
            raw_output = await self._harness.execute(node.tool, args)
        except ToolExecutionError as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        if raw_output is None or raw_output in ({}, []):
            log.info("Node %d: tool '%s' returned empty", node.id, node.tool)
            return NodeResult(
                node_id=node.id, status=NodeStatus.EMPTY, output=raw_output, args_used=args
            )

        ok, issue = await self._check_output_shape(node, raw_output, tier)
        if not ok:
            log.warning("Node %d unexpected result shape: %s", node.id, issue)
            return NodeResult(
                node_id=node.id,
                status=NodeStatus.COMPLETED,
                output=raw_output,
                args_used=args,
                confidence=0.5,
                flagged=True,
                error=f"Unexpected shape: {issue}",
            )

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=raw_output,
            args_used=args,
            confidence=1.0,
        )

    async def _execute_decision(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
    ) -> NodeResult:
        ctx = assemble_context(
            node,
            state.results,
            dependent_nodes=self._direct_dependents(node, state) or None,
        )
        allowed = list(node.branches.keys()) if node.branches else ["yes", "no"]
        messages: list[Message] = [
            {"role": "system", "content": self._settings.prompts.decision},
            {
                "role": "user",
                "content": (
                    f"Condition to evaluate: {node.condition}\n"
                    f"Allowed branches: {allowed}\n\n"
                    f"Available context:\n{ctx}"
                ),
            },
        ]

        tokens = 0
        attempt_tier = tier
        max_retries = self._max_llm_node_retries()
        attempt = 0
        for attempt in range(max_retries + 1):
            try:
                raw, attempt_tokens = await self._collect_retryable_output(
                    self._router.complete(
                        messages,
                        min_tier=attempt_tier,
                        json_mode=True,
                        max_tokens=self._settings.budgets.decision,
                    )
                )
                tokens += attempt_tokens
                data = json.loads(strip_model_output(raw))
                branch = data.get("branch", node.default_branch or "yes")
                confidence = _clamp_confidence(float(data.get("confidence", 0.5)), 0.5, node.id)
                if node.branches and branch not in node.branches:
                    log.warning(
                        "Node %d returned unknown branch '%s', falling back to default '%s'",
                        node.id,
                        branch,
                        node.default_branch,
                    )
                    branch = node.default_branch or next(iter(node.branches))
                return NodeResult(
                    node_id=node.id,
                    status=NodeStatus.COMPLETED,
                    branch_taken=branch,
                    confidence=confidence,
                    tokens_used=tokens,
                    retries=attempt,
                    tier_used=attempt_tier,
                )
            except _EmptyLLMOutputError as exc:
                tokens += exc.tokens_used
                if attempt < max_retries:
                    next_tier = self._retry_tier(attempt_tier)
                    self._log_llm_retry(node, attempt, attempt_tier, next_tier, exc)
                    attempt_tier = next_tier
                    continue
                log.warning("Decision node %d failed (%s), using default branch", node.id, exc)
                break
            except ProviderUnavailableError as exc:
                if attempt < max_retries:
                    next_tier = self._retry_tier(attempt_tier)
                    self._log_llm_retry(node, attempt, attempt_tier, next_tier, exc)
                    attempt_tier = next_tier
                    continue
                log.warning("Decision node %d failed (%s), using default branch", node.id, exc)
                break
            except Exception as exc:
                log.warning("Decision node %d failed (%s), using default branch", node.id, exc)
                break

        branch = node.default_branch or (next(iter(node.branches)) if node.branches else "yes")
        confidence = 0.0

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            branch_taken=branch,
            confidence=confidence,
            tokens_used=tokens,
            retries=min(attempt, max_retries),
            tier_used=attempt_tier,
        )

    async def _execute_llm_node(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
        *,
        spec: _LLMNodeSpec,
        bus: EventBus | None = None,
        node_path: str | None = None,
    ) -> NodeResult:
        """Run a single-LLM-call node (synthesis / think / summary / result).

        These node types differ only in their system prompt, the label they
        give the upstream context, which JSON field carries their output, and
        their default confidence — all supplied by ``spec``.

        For streaming node types (think / synthesis / summary), each chunk of
        the output field is published as a ``NodeOutputChunk`` event so
        subscribers (e.g. the ACP adapter) can forward it to the client in
        real time.
        """
        result = NodeResult(
            node_id=node.id,
            status=NodeStatus.FAILED,
            error="LLM node did not run",
        )
        tokens = 0
        attempt_tier = tier
        max_retries = self._max_llm_node_retries()
        for attempt in range(max_retries + 1):
            try:
                result = await self._execute_llm_node_once(
                    node,
                    state,
                    attempt_tier,
                    spec=spec,
                    bus=bus,
                    node_path=node_path,
                )
            except _EmptyLLMOutputError as exc:
                tokens += exc.tokens_used
                result = NodeResult(
                    node_id=node.id,
                    status=NodeStatus.FAILED,
                    tokens_used=tokens,
                    retries=attempt,
                    tier_used=attempt_tier,
                    error=str(exc),
                )
                if attempt < max_retries:
                    next_tier = self._retry_tier(attempt_tier)
                    self._log_llm_retry(node, attempt, attempt_tier, next_tier, exc)
                    attempt_tier = next_tier
                    continue
                return result
            except ProviderUnavailableError as exc:
                result = NodeResult(
                    node_id=node.id,
                    status=NodeStatus.FAILED,
                    tokens_used=tokens,
                    retries=attempt,
                    tier_used=attempt_tier,
                    error=str(exc),
                )
                if attempt < max_retries:
                    next_tier = self._retry_tier(attempt_tier)
                    self._log_llm_retry(node, attempt, attempt_tier, next_tier, exc)
                    attempt_tier = next_tier
                    continue
                return result

            result.tokens_used += tokens
            result.retries = attempt
            result.tier_used = attempt_tier
            if result.status is NodeStatus.COMPLETED:
                return result
            return result
        return result

    async def _execute_llm_node_once(
        self,
        node: Node,
        state: ExecutionState,
        tier: int,
        *,
        spec: _LLMNodeSpec,
        bus: EventBus | None = None,
        node_path: str | None = None,
    ) -> NodeResult:
        dependents = self._direct_dependents(node, state)
        ctx = assemble_context(node, state.results, dependent_nodes=dependents or None)
        # For the result node, prepend the original user request so the model
        # knows what question it is directly answering (each node runs with fresh
        # context and would otherwise only see the planner-written description).
        task_prefix = (
            f"Original user request: {state.user_request}\n\n"
            if node.type is NodeType.RESULT
            else ""
        )
        messages: list[Message] = [
            {"role": "system", "content": self._node_system_prompt(node)},
            {
                "role": "user",
                "content": f"{task_prefix}Task: {node.description}\n\n{spec.context_label}\n{ctx}",
            },
        ]
        stream = self._router.complete(
            messages,
            min_tier=tier,
            json_mode=True,
            max_tokens=self._node_output_budget(node.type),
        )
        try:
            if node.type in _STREAMING_NODE_TYPES and bus is not None:
                raw, tokens = await self._stream_llm_node(
                    node, state, stream, spec, bus, node_path=node_path
                )
            else:
                raw, tokens = await self._collect_retryable_output(stream)
            if not raw.strip():
                raise _EmptyLLMOutputError(tokens_used=tokens)
            try:
                data = json.loads(strip_model_output(raw))
            except json.JSONDecodeError:
                # Local models occasionally wrap their JSON in prose or leave the
                # object unclosed. Try to salvage the output field via the streaming
                # extractor before giving up entirely.
                streamer = JsonFieldStreamer(spec.output_key)
                salvaged = streamer.feed(raw)
                # Use `done` (field marker found AND closing quote seen) OR a
                # non-empty partial (truncated but usable). Do NOT use truthiness
                # alone: an empty field value is valid and should not cause a failure.
                if streamer.done or salvaged:
                    log.warning(
                        "Node %d: JSON parse failed; salvaged %r field via streamer "
                        "(done=%s, len=%d) — confidence will default to %.2f",
                        node.id,
                        spec.output_key,
                        streamer.done,
                        len(salvaged),
                        spec.default_confidence,
                    )
                    data = {spec.output_key: salvaged}
                else:
                    raise
            raw_output = data.get(spec.output_key)
            if raw_output is None:
                log.warning(
                    "Node %d: response missing expected key %r; falling back to raw output",
                    node.id,
                    spec.output_key,
                )
            output = raw_output if raw_output is not None else raw
            raw_confidence = float(data.get("confidence", spec.default_confidence))
            confidence = _clamp_confidence(raw_confidence, spec.default_confidence, node.id)
        except (ProviderUnavailableError, _EmptyLLMOutputError):
            raise
        except Exception as exc:
            return NodeResult(node_id=node.id, status=NodeStatus.FAILED, error=str(exc))

        return NodeResult(
            node_id=node.id,
            status=NodeStatus.COMPLETED,
            output=output,
            confidence=confidence,
            tokens_used=tokens,
        )

    @staticmethod
    async def _collect_retryable_output(stream: AsyncIterator[Chunk]) -> tuple[str, int]:
        raw, tokens = await collect(stream)
        if not raw.strip():
            raise _EmptyLLMOutputError(tokens_used=tokens)
        return raw, tokens

    def _max_llm_node_retries(self) -> int:
        return max(0, self._settings.resilience.max_llm_node_retries)

    def _retry_tier(self, previous_tier: int) -> int:
        if not self._settings.resilience.escalate_tier_on_retry:
            return previous_tier
        return min(previous_tier + 1, self._highest_configured_tier())

    def _highest_configured_tier(self) -> int:
        return max(provider.tier for provider in self._router.providers)

    @staticmethod
    def _log_llm_retry(
        node: Node,
        attempt: int,
        prior_tier: int,
        next_tier: int,
        exc: Exception,
    ) -> None:
        log.warning(
            "Node %d LLM attempt %d failed at tier %d (%s); retrying at tier %d",
            node.id,
            attempt + 1,
            prior_tier,
            exc,
            next_tier,
            extra={
                "node_id": node.id,
                "attempt": attempt + 1,
                "prior_tier": prior_tier,
                "next_tier": next_tier,
                "error": str(exc),
            },
        )

    async def _stream_llm_node(
        self,
        node: Node,
        state: ExecutionState,
        stream: AsyncIterator[Chunk],
        spec: _LLMNodeSpec,
        bus: EventBus,
        *,
        node_path: str | None = None,
    ) -> tuple[str, int]:
        """Collect a streaming LLM response, publishing NodeOutputChunk events.

        Extracts the named output field from the streaming JSON and emits each
        fragment as it arrives. Returns the full raw text and token count for
        the caller to parse into a final NodeResult.
        """
        streamer = JsonFieldStreamer(spec.output_key)
        parts: list[str] = []
        tokens = 0
        async for chunk in stream:
            if chunk.text:
                parts.append(chunk.text)
                new_text = streamer.feed(chunk.text)
                if new_text:
                    await self._emit(
                        bus,
                        NodeOutputChunk(
                            task_id=state.task_id,
                            node_id=node.id,
                            node_type=node.type,
                            text=new_text,
                            node_path=node_path,
                        ),
                    )
            if chunk.tokens_used is not None:
                tokens = chunk.tokens_used
        return "".join(parts), tokens

    async def _execute_subplan(
        self,
        node: Node,
        state: ExecutionState,
        *,
        semaphore: asyncio.Semaphore,
        emit_nested: bool,
        path_prefix: str,
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

        ctx = assemble_context(
            node,
            state.results,
            dependent_nodes=self._direct_dependents(node, state) or None,
        )
        sub_request = f"Subplan task: {node.description}\n\nParent task:\n{state.user_request}"
        if ctx:
            sub_request += f"\n\nContext from parent plan:\n{ctx}"
        sub_state = ExecutionState(
            task_id=state.task_id,
            user_request=sub_request,
            plan=node.subplan,
        )
        try:
            completed = await self._run(
                sub_state,
                emit=emit_nested,
                semaphore=semaphore,
                path_prefix=self._child_path(path_prefix, node.id),
                emit_task_completed=False,
            )
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

    @staticmethod
    def _node_path(path_prefix: str, node_id: int) -> str | None:
        """External event path for a node, or None for top-level nodes."""
        return f"{path_prefix}/{node_id}" if path_prefix else None

    @staticmethod
    def _child_path(path_prefix: str, node_id: int) -> str:
        """Prefix to pass into a nested plan rooted at ``node_id``."""
        return f"{path_prefix}/{node_id}" if path_prefix else str(node_id)

    async def _approve_tool_call(
        self,
        *,
        node: Node,
        state: ExecutionState,
        args: dict[str, object],
    ) -> bool:
        """Apply configured per-tool approval policy before execution."""
        assert node.tool is not None
        policy = self._effective_tool_policy(node.tool)
        if policy is ToolApprovalPolicy.DENY:
            log.warning("Node %d: tool '%s' denied by policy", node.id, node.tool)
            return False
        if policy is ToolApprovalPolicy.ALLOW:
            return True
        if self._settings.tool_approval.mode is ToolApprovalMode.AUTO:
            return True
        if self._tool_approval is None:
            log.warning(
                "Node %d: tool '%s' requires approval but no channel exists",
                node.id,
                node.tool,
            )
            return False
        if self._tool_approval.is_always_allowed(node.tool):
            return True
        return await self._tool_approval.request(
            task_id=state.task_id,
            node_id=node.id,
            tool_name=node.tool,
            risk=self._harness.risk_for(node.tool),
            args=args,
            description=node.description,
        )

    def _effective_tool_policy(self, tool_name: str) -> ToolApprovalPolicy:
        """Resolve explicit approval config plus risk-derived defaults."""
        override = self._settings.tool_approval.overrides.get(tool_name)
        if override is not None:
            return override
        if self._harness.risk_for(tool_name) in {"write", "execute"}:
            return ToolApprovalPolicy.ASK
        return self._settings.tool_approval.default

    @staticmethod
    def _direct_dependents(node: Node, state: ExecutionState) -> list[Node]:
        """Nodes that directly consume ``node`` via ``context_needed``.

        This is planning metadata only: it helps the current node shape its
        output for consumers without granting access to any additional results.
        """
        plan = state.plan
        if plan is None:
            return []
        return [n for n in plan.nodes if node.id in n.context_needed and n.id != node.id]

    def _node_output_budget(self, node_type: NodeType) -> int:
        """Output ``max_tokens`` for a single-LLM-call content node.

        Maps the four content node types onto their configured budgets; the
        structured micro-calls (decision, arg extraction, shape check) read
        their budgets directly off ``settings.budgets``.
        """
        budgets = self._settings.budgets
        return {
            NodeType.THINK: budgets.think,
            NodeType.SUMMARY: budgets.summary,
            NodeType.SYNTHESIS: budgets.synthesis,
            NodeType.RESULT: budgets.result,
        }[node_type]

    def _node_system_prompt(self, node: Node) -> str:
        """System prompt for a single-LLM-call content node, from settings."""
        prompts = self._settings.prompts
        if node.type is NodeType.SUMMARY:
            if node.format is SummaryFormat.CONCISE:
                return prompts.summary_concise
            if node.format is SummaryFormat.VERBOSE:
                return prompts.summary_verbose
            return prompts.summary
        return {
            NodeType.THINK: prompts.think,
            NodeType.SYNTHESIS: prompts.synthesis,
            NodeType.RESULT: prompts.result,
        }[node.type]

    def _select_tier(self, node: Node) -> int:
        """Pick a tier for ``node``. Deterministic — the model never decides."""
        if node.forced_tier is not None:
            return node.forced_tier

        routing = self._settings.routing
        base = {
            NodeType.DECISION: routing.decision,
            NodeType.TOOL: routing.tool,
            NodeType.SYNTHESIS: routing.synthesis,
            NodeType.THINK: routing.think,
            NodeType.SUMMARY: routing.summary,
            NodeType.RESULT: routing.result,
        }.get(node.type, 0)
        if node.reasoning_required:
            base = max(base, routing.reasoning_required)
        return base

    async def _extract_args(
        self,
        node: Node,
        ctx: str,
        tier: int,
    ) -> dict[str, object]:
        schema = self._harness.schema_for(node.tool or "")
        prompt = f"Tool: {node.tool}\nTool schema: {json.dumps(schema)}\n\nContext:\n{ctx}"
        try:
            raw, _ = await collect(
                self._router.complete(
                    [
                        {"role": "system", "content": self._settings.prompts.extract_args},
                        {"role": "user", "content": prompt},
                    ],
                    min_tier=tier,
                    json_mode=True,
                    max_tokens=self._settings.budgets.extract_args,
                )
            )
            data = json.loads(strip_model_output(raw))
            if isinstance(data, dict):
                return cast("dict[str, object]", data)
            return {}
        except Exception:
            return {}

    async def _check_output_shape(
        self,
        node: Node,
        output: object,
        tier: int,
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
                        {"role": "system", "content": self._settings.prompts.shape_check},
                        {"role": "user", "content": prompt},
                    ],
                    min_tier=tier,
                    json_mode=True,
                    max_tokens=self._settings.budgets.shape_check,
                )
            )
            data = json.loads(strip_model_output(raw))
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
    ) -> set[int]:
        skipped: set[int] = set()
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
        return skipped

    def _extract_final_output(self, state: ExecutionState, plan: Plan) -> str | None:
        """The last completed RESULT node's output is the final answer; if
        the plan declared no RESULT node, fall back to the last completed
        synthesis/summary node so older or imperfect plans keep producing the
        best completed answer they have."""
        for target in (NodeType.RESULT, NodeType.SYNTHESIS, NodeType.SUMMARY):
            for node_id in reversed(plan.sorted_node_ids):
                node = next((n for n in plan.nodes if n.id == node_id), None)
                if node is None or node.type is not target:
                    continue
                if self._only_uses_skipped_content(node, state, plan):
                    continue
                result = state.results.get(node_id)
                if result and result.status is NodeStatus.COMPLETED:
                    return str(result.output)
        return None

    @staticmethod
    def _dependency_map(plan: Plan) -> dict[int, set[int]]:
        """All data and decision-control dependencies for scheduler readiness."""
        deps = {node.id: set(node.context_needed) for node in plan.nodes}
        for node in plan.nodes:
            if node.type is not NodeType.DECISION or not node.branches:
                continue
            for branch_nodes in node.branches.values():
                for target_id in branch_nodes:
                    if target_id in deps:
                        deps[target_id].add(node.id)
        return deps

    @staticmethod
    def _content_dependencies(node: Node, plan: Plan) -> list[int]:
        node_map = {n.id: n for n in plan.nodes}
        return [
            dep
            for dep in node.context_needed
            if node_map.get(dep, node).type is not NodeType.DECISION
        ]

    @staticmethod
    def _only_uses_skipped_content(
        node: Node,
        state: ExecutionState,
        plan: Plan,
        *,
        missing_counts_as_skipped: bool = True,
    ) -> bool:
        """True when all non-decision inputs to a final candidate were skipped.

        This keeps an untaken branch's downstream result node from winning the
        final answer while still allowing shared join nodes that consume one
        completed branch and one skipped branch.
        """
        if not node.context_needed:
            return False
        content_deps = Executor._content_dependencies(node, plan)
        if not content_deps:
            return False
        for dep in content_deps:
            result = state.results.get(dep)
            if result is None:
                if missing_counts_as_skipped:
                    continue
                return False
            if result.status is not NodeStatus.SKIPPED:
                return False
        return True
