"""Orchestrator — wraps planning, optional HITL gate, and execution.

Owns the persistence handoffs: it saves state after planning, after the
optional confirmation gate, and after execution. The :class:`Executor` is
free to mutate the in-memory ``ExecutionState`` directly; the orchestrator
takes care of writing it down.
"""

from __future__ import annotations

import asyncio
import logging
import re

from opentelemetry.trace import Status, StatusCode

from glassrail.config import Settings
from glassrail.core import (
    ExecutionState,
    NodeType,
    Plan,
    PlanningAttempt,
    PlanValidationError,
    TaskId,
    TaskStatus,
)
from glassrail.events import (
    AwaitingConfirmation,
    Event,
    EventBus,
    PlanFailed,
    PlanningStarted,
    PlanReady,
    PlanRejected,
    TaskCancelled,
    TaskFailed,
)
from glassrail.executor.executor import Executor
from glassrail.planner import Planner, rejection_retry_feedback
from glassrail.state import StateStore
from glassrail.telemetry import (
    ATTR_PLAN_REJECTION_REASON,
    ATTR_TASK_ID,
    ATTR_TASK_STATUS,
    SPAN_TASK,
    get_tracer,
)

log = logging.getLogger(__name__)

_BINARY_QUESTION_RE = re.compile(
    r"\b(?:is|are|was|were|does|do|did|will|would|should|has|have|can|could)\b"
    r"[^.?!]{0,80}?\s+or\s+[^.?!]{1,40}?\?",
    re.IGNORECASE,
)
_COMPARISON_REQUEST_MARKERS = (
    "recommend",
    "compare",
    " vs ",
    " vs.",
    "versus",
    "trade-off",
    "tradeoff",
)


class Orchestrator:
    """Top-level coordinator for the full plan-validate-confirm-execute flow."""

    def __init__(
        self,
        *,
        planner: Planner,
        executor: Executor,
        state_store: StateStore,
        settings: Settings,
        event_bus: EventBus | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._store = state_store
        self._settings = settings
        self._bus = event_bus

    async def run(self, task_id: TaskId) -> None:
        with get_tracer().start_as_current_span(SPAN_TASK) as span:
            span.set_attribute(ATTR_TASK_ID, str(task_id))
            state = await self._store.load_task(task_id)
            if state is None:
                log.warning("Orchestrator.run: task %s not found", task_id)
                return

            await self._emit(PlanningStarted(task_id=task_id))
            try:
                plan = await self._plan_with_retry(state)
                if plan is None:
                    await self._emit_plan_terminal(state)
                    await self._store.save_task(state)
                    return

                state.plan = plan
                log.info("[%s] Plan validated: %d nodes", task_id, len(plan.nodes))
                await self._present_or_execute(state)
            except asyncio.CancelledError:
                await self._mark_cancelled(state)
                raise
            except Exception as exc:
                log.exception("[%s] Unhandled error: %s", task_id, exc)
                state.status = TaskStatus.FAILED
                state.error = str(exc)
                state.touch()
                await self._emit(
                    TaskFailed(
                        task_id=task_id,
                        error=str(exc),
                        attempts=[a.model_dump(mode="json") for a in state.planning_attempts],
                    )
                )
            finally:
                await self._store.save_task(state)
                span.set_attribute(ATTR_TASK_STATUS, state.status.value)
                if state.status is TaskStatus.REJECTED and state.error:
                    span.set_attribute(ATTR_PLAN_REJECTION_REASON, state.error)
                if state.status is TaskStatus.FAILED:
                    span.set_status(Status(StatusCode.ERROR, state.error or "task failed"))

    async def execute_plan(self, state: ExecutionState) -> None:
        """Execute a pre-built plan, bypassing the planner entirely.

        Used by ``glassrail exec-plan`` to inject a fixed plan JSON and run
        only the executor.  The caller is responsible for validating the plan
        and setting ``state.plan`` before calling.

        Note: the executor sets ``state.status = COMPLETED`` unconditionally
        after the node loop, even when individual nodes failed.  Top-level
        ``status``/``is_error`` in the envelope is therefore not a reliable
        pass/fail signal for harness grading — inspect the per-node
        ``status`` fields in ``trajectory`` instead.
        """
        assert state.plan is not None, "execute_plan requires state.plan to be set"
        try:
            await self._executor.execute(state)
        except Exception as exc:
            log.exception("[%s] exec-plan failed: %s", state.task_id, exc)
            state.status = TaskStatus.FAILED
            state.error = str(exc)
            state.touch()
        finally:
            await self._store.save_task(state)

    async def resume(self, task_id: TaskId) -> None:
        """Resume a task paused at confirmation or mid-execution."""
        with get_tracer().start_as_current_span(SPAN_TASK) as span:
            span.set_attribute(ATTR_TASK_ID, str(task_id))
            state = await self._store.load_task(task_id)
            if state is None:
                log.warning("Orchestrator.resume: task %s not found", task_id)
                return
            if state.status not in (
                TaskStatus.AWAITING_CONFIRMATION,
                TaskStatus.PAUSED,
                TaskStatus.EXECUTING,
            ):
                log.warning(
                    "Orchestrator.resume: task %s is in status %s, not resumable",
                    task_id,
                    state.status,
                )
                return

            try:
                await self._executor.execute(state)
            except asyncio.CancelledError:
                await self._mark_cancelled(state)
                raise
            except Exception as exc:
                log.exception("[%s] Resume failed: %s", task_id, exc)
                state.status = TaskStatus.FAILED
                state.error = str(exc)
                state.touch()
                await self._emit(
                    TaskFailed(
                        task_id=task_id,
                        error=str(exc),
                        attempts=[a.model_dump(mode="json") for a in state.planning_attempts],
                    )
                )
            finally:
                await self._store.save_task(state)
                span.set_attribute(ATTR_TASK_STATUS, state.status.value)
                if state.status is TaskStatus.FAILED:
                    span.set_status(Status(StatusCode.ERROR, state.error or "task failed"))

    async def revise(self, task_id: TaskId, feedback: str) -> None:
        """Re-plan a task paused at the gate, guided by the user's feedback.

        Drives the reject-with-feedback path: from an ``AWAITING_CONFIRMATION``
        state, the planner produces a fresh plan that addresses ``feedback`` and
        the orchestrator re-enters the confirmation gate (or executes, if the
        gate is off).
        """
        with get_tracer().start_as_current_span(SPAN_TASK) as span:
            span.set_attribute(ATTR_TASK_ID, str(task_id))
            state = await self._store.load_task(task_id)
            if state is None:
                log.warning("Orchestrator.revise: task %s not found", task_id)
                return
            if state.status not in (TaskStatus.AWAITING_CONFIRMATION, TaskStatus.PAUSED):
                log.warning(
                    "Orchestrator.revise: task %s is in status %s, not revisable",
                    task_id,
                    state.status,
                )
                return

            await self._emit(PlanningStarted(task_id=task_id))
            rejection_reason: str | None = None
            try:
                plan = await self._plan_with_retry(state, feedback=feedback)
                if plan is None:
                    # Use the last planning attempt's error_type rather than
                    # state.status to detect a rejection. Pyright narrows
                    # state.status to AWAITING_CONFIRMATION | PAUSED from the
                    # top-of-method guard and doesn't track _plan_with_retry's
                    # mutation, so a status comparison would be a false positive.
                    last = state.planning_attempts[-1] if state.planning_attempts else None
                    if last is not None and last.error_type == "rejection":
                        rejection_reason = last.error
                    await self._emit_plan_terminal(state)
                    await self._store.save_task(state)
                    return
                state.plan = plan
                log.info("[%s] Plan revised: %d nodes", task_id, len(plan.nodes))
                await self._present_or_execute(state)
            except asyncio.CancelledError:
                await self._mark_cancelled(state)
                raise
            except Exception as exc:
                log.exception("[%s] Revise failed: %s", task_id, exc)
                state.status = TaskStatus.FAILED
                state.error = str(exc)
                state.touch()
                await self._emit(TaskFailed(task_id=task_id, error=str(exc)))
            finally:
                await self._store.save_task(state)
                span.set_attribute(ATTR_TASK_STATUS, state.status.value)
                if rejection_reason is not None:
                    span.set_attribute(ATTR_PLAN_REJECTION_REASON, rejection_reason)
                if state.status is TaskStatus.FAILED:
                    span.set_status(Status(StatusCode.ERROR, state.error or "task failed"))

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _emit(self, event: Event) -> None:
        if self._bus is not None:
            await self._bus.publish(event)

    async def _emit_plan_terminal(self, state: ExecutionState) -> None:
        """Emit the appropriate terminal planning event based on state status."""
        if state.status is TaskStatus.REJECTED:
            await self._emit(PlanRejected(task_id=state.task_id, reason=state.error or "rejected"))
        else:
            # Surface the filepath from the last attempt that was written to disk,
            # so the TUI (and logs) can tell the user where to find the raw output.
            last_filepath = next(
                (a.filepath for a in reversed(state.planning_attempts) if a.filepath),
                None,
            )
            await self._emit(
                PlanFailed(
                    task_id=state.task_id,
                    error=state.error or "planning failed",
                    attempts=[a.model_dump(mode="json") for a in state.planning_attempts],
                    filepath=last_filepath,
                )
            )

    async def _mark_cancelled(self, state: ExecutionState) -> None:
        """Record a cancelled task and emit the terminal event.

        Runs in the ``CancelledError`` handler before re-raising; the method's
        ``finally`` then persists the cancelled state. A single cancel() is
        assumed (the ACP adapter cancels once), so these awaits complete.
        """
        state.status = TaskStatus.CANCELLED
        if state.error is None:
            state.error = "cancelled"
        state.touch()
        await self._emit(TaskCancelled(task_id=state.task_id))

    async def _save_planning_attempt(
        self,
        state: ExecutionState,
        plan_attempt: PlanningAttempt,
    ) -> None:
        state.planning_attempts.append(plan_attempt)
        state.touch()
        await self._store.save_task(state)

    async def _mark_plan_rejected(
        self,
        state: ExecutionState,
        plan_attempt: PlanningAttempt,
    ) -> None:
        log.warning("[%s] Task rejected by planner: %s", state.task_id, plan_attempt.error)
        state.status = TaskStatus.REJECTED
        state.error = plan_attempt.error
        state.touch()

    def _answerable_rejection_retry_feedback(
        self,
        state: ExecutionState,
        plan_attempt: PlanningAttempt,
        *,
        attempt: int,
        attempts: int,
    ) -> str | None:
        retry_feedback = rejection_retry_feedback(plan_attempt.error)
        if retry_feedback is None or attempt >= attempts - 1:
            return None
        log.warning(
            "[%s] Planner rejected an answerable task (attempt %d); retrying",
            state.task_id,
            attempt,
        )
        return retry_feedback

    async def _present_or_execute(self, state: ExecutionState) -> None:
        """Announce the validated plan, then gate on confirmation or execute.

        Shared by the initial run and the guided-replan path so the
        PlanReady → (gate | execute) sequence stays identical for both.
        """
        plan = state.plan
        assert plan is not None
        await self._emit(
            PlanReady(
                task_id=state.task_id,
                node_count=len(plan.nodes),
                plan=plan.model_dump(mode="json"),
            )
        )
        if self._settings.confirm_plans:
            state.status = TaskStatus.AWAITING_CONFIRMATION
            state.touch()
            await self._store.save_task(state)
            await self._emit(
                AwaitingConfirmation(task_id=state.task_id, node_count=len(plan.nodes))
            )
            log.info(
                "[%s] Plan summary (awaiting confirmation):\n%s", state.task_id, _summary(plan)
            )
            return
        await self._executor.execute(state)
        log.info("[%s] Completed", state.task_id)

    async def _plan_with_retry(
        self, state: ExecutionState, *, feedback: str | None = None
    ) -> Plan | None:
        state.status = TaskStatus.PLANNING
        state.touch()
        log.info("[%s] Planning...", state.task_id)

        attempts = self._settings.max_replan_attempts + 1
        last_error: str | None = None
        last_error_type: str | None = None
        # Carries raw output from a previous planner stall so the next attempt
        # can explicitly avoid repeating it.
        prior_reasoning: str | None = None
        validation_feedback: str | None = None
        rejected_answer_feedback: str | None = None

        for attempt in range(attempts):
            try:
                plan_attempt = await self._planner.plan_attempt(
                    state.user_request,
                    attempt=attempt,
                    min_tier=self._settings.planner_min_tier,
                    feedback=feedback,
                    prior_reasoning=prior_reasoning,
                    validation_feedback=validation_feedback,
                    rejection_feedback=rejected_answer_feedback,
                    # Enable thinking on retry only when the previous attempt
                    # failed for a non-timeout reason.  Thinking makes the
                    # planner slower; retrying a timeout with thinking enabled
                    # just burns the full retry budget for a near-certain second
                    # timeout.  On a stall, rejection, or parse failure, thinking
                    # can genuinely help.
                    thinking=attempt > 0 and last_error_type != "timeout",
                )
                state.replan_count = attempt
                state.touch()

                if plan_attempt.plan is not None:
                    structural_feedback = _structural_retry_feedback(
                        state.user_request,
                        plan_attempt.plan,
                    )
                    if structural_feedback is not None and attempt < attempts - 1:
                        plan_attempt = plan_attempt.model_copy(
                            update={
                                "plan": None,
                                "error": structural_feedback,
                                "error_type": "validation",
                            }
                        )
                        await self._save_planning_attempt(state, plan_attempt)
                        validation_feedback = structural_feedback
                        prior_reasoning = None
                        last_error = structural_feedback
                        last_error_type = "validation"
                        log.warning(
                            "[%s] Plan structurally suspicious (attempt %d): %s",
                            state.task_id,
                            attempt,
                            structural_feedback,
                        )
                        continue

                    await self._save_planning_attempt(state, plan_attempt)
                    return plan_attempt.plan

                await self._save_planning_attempt(state, plan_attempt)

                if plan_attempt.error_type == "rejection":
                    retry_feedback = self._answerable_rejection_retry_feedback(
                        state,
                        plan_attempt,
                        attempt=attempt,
                        attempts=attempts,
                    )
                    if retry_feedback is not None:
                        rejected_answer_feedback = retry_feedback
                        prior_reasoning, validation_feedback = None, None
                        last_error = plan_attempt.error
                        last_error_type = "rejection"
                        continue

                    # Deliberate planner decision or retry budget exhausted.
                    await self._mark_plan_rejected(state, plan_attempt)
                    return None

                prior_reasoning, validation_feedback = _planning_retry_context(plan_attempt)
                rejected_answer_feedback = None

                last_error = plan_attempt.error
                last_error_type = plan_attempt.error_type
                log.warning(
                    "[%s] Plan invalid (attempt %d): %s",
                    state.task_id,
                    attempt,
                    plan_attempt.error,
                )
            except (PlanValidationError, ValueError) as exc:
                last_error = str(exc)
                last_error_type = "validation"
                prior_reasoning = None
                validation_feedback = str(exc)
                log.warning(
                    "[%s] Plan invalid (attempt %d): %s",
                    state.task_id,
                    attempt,
                    exc,
                )

        state.status = TaskStatus.FAILED
        state.error = f"Planning failed after {attempts} attempts: {last_error}"
        state.touch()
        return None


def _summary(plan: Plan) -> str:
    return "\n".join(f"  {n.id}. [{n.type}] {n.description}" for n in plan.nodes)


def _planning_retry_context(plan_attempt: PlanningAttempt) -> tuple[str | None, str | None]:
    prior_reasoning = (
        plan_attempt.error_detail or plan_attempt.raw_output
        if plan_attempt.error_type == "stall"
        else None
    )
    validation_feedback = (
        plan_attempt.error if plan_attempt.error_type in ("schema", "validation") else None
    )
    return prior_reasoning, validation_feedback


def _structural_retry_feedback(request: str, plan: Plan) -> str | None:
    if _looks_like_conditional_request(request) and not any(
        node.type is NodeType.DECISION for node in plan.nodes
    ):
        return (
            "The request contains conditional if/otherwise or binary branch "
            "logic, but the plan has no decision node. Re-plan with an explicit "
            "decision node whose yes/no branches perform the conditional work."
        )
    return None


def _looks_like_conditional_request(request: str) -> bool:
    text = f" {request.lower()} "
    conditional_markers = (
        " otherwise ",
        " else ",
        " whether ",
    )
    if any(marker in text for marker in conditional_markers):
        return True
    if _BINARY_QUESTION_RE.search(request) and not any(
        marker in text for marker in _COMPARISON_REQUEST_MARKERS
    ):
        return True

    meaningful_if_count = 0
    optional_if_prefixes = (
        "if present",
        "if available",
        "if any",
        "if applicable",
        "if found",
        "if included",
        "if known",
        "if mentioned",
        "if possible",
        "if provided",
        "if stated",
    )
    branch_verbs = (
        "calculate",
        "choose",
        "compute",
        "give",
        "read",
        "recommend",
        "report",
        "return",
        "say",
        "tell",
        "use",
        "write",
    )
    for match in re.finditer(r"\bif\b", text):
        suffix = text[match.start() : match.start() + 120]
        if suffix.startswith(optional_if_prefixes):
            continue
        meaningful_if_count += 1
        if re.search(r"\bthen\b", suffix) or re.search(
            rf",\s*(?:{'|'.join(branch_verbs)})\b", suffix
        ):
            return True

    return meaningful_if_count >= 2
