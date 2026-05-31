"""Orchestrator — wraps planning, optional HITL gate, and execution.

Owns the persistence handoffs: it saves state after planning, after the
optional confirmation gate, and after execution. The :class:`Executor` is
free to mutate the in-memory ``ExecutionState`` directly; the orchestrator
takes care of writing it down.
"""

from __future__ import annotations

import logging

from opentelemetry.trace import Status, StatusCode

from dagagent.config import Settings
from dagagent.core import (
    ExecutionState,
    Plan,
    PlanValidationError,
    TaskId,
    TaskStatus,
)
from dagagent.events import (
    AwaitingConfirmation,
    Event,
    EventBus,
    PlanFailed,
    PlanningStarted,
    PlanReady,
    TaskFailed,
)
from dagagent.executor.executor import Executor
from dagagent.planner import Planner
from dagagent.state import StateStore
from dagagent.telemetry import ATTR_TASK_ID, ATTR_TASK_STATUS, SPAN_TASK, get_tracer

log = logging.getLogger(__name__)


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
                    await self._emit(
                        PlanFailed(
                            task_id=task_id,
                            error=state.error or "planning failed",
                            attempts=[a.model_dump(mode="json") for a in state.planning_attempts],
                        )
                    )
                    await self._store.save_task(state)
                    return

                state.plan = plan
                log.info("[%s] Plan validated: %d nodes", task_id, len(plan.nodes))
                await self._present_or_execute(state)
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
                if state.status is TaskStatus.FAILED:
                    span.set_status(Status(StatusCode.ERROR, state.error or "task failed"))

    async def resume(self, task_id: TaskId) -> None:
        """Resume a task paused at confirmation or mid-execution."""
        with get_tracer().start_as_current_span(SPAN_TASK) as span:
            span.set_attribute(ATTR_TASK_ID, str(task_id))
            state = await self._store.load_task(task_id)
            if state is None:
                log.warning("Orchestrator.resume: task %s not found", task_id)
                return
            if state.status not in (TaskStatus.AWAITING_CONFIRMATION, TaskStatus.PAUSED):
                log.warning(
                    "Orchestrator.resume: task %s is in status %s, not resumable",
                    task_id,
                    state.status,
                )
                return

            try:
                await self._executor.execute(state)
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
            try:
                plan = await self._plan_with_retry(state, feedback=feedback)
                if plan is None:
                    await self._emit(
                        PlanFailed(
                            task_id=task_id,
                            error=state.error or "planning failed",
                            attempts=[a.model_dump(mode="json") for a in state.planning_attempts],
                        )
                    )
                    await self._store.save_task(state)
                    return
                state.plan = plan
                log.info("[%s] Plan revised: %d nodes", task_id, len(plan.nodes))
                await self._present_or_execute(state)
            except Exception as exc:
                log.exception("[%s] Revise failed: %s", task_id, exc)
                state.status = TaskStatus.FAILED
                state.error = str(exc)
                state.touch()
                await self._emit(TaskFailed(task_id=task_id, error=str(exc)))
            finally:
                await self._store.save_task(state)
                span.set_attribute(ATTR_TASK_STATUS, state.status.value)
                if state.status is TaskStatus.FAILED:
                    span.set_status(Status(StatusCode.ERROR, state.error or "task failed"))

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _emit(self, event: Event) -> None:
        if self._bus is not None:
            await self._bus.publish(event)

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

        for attempt in range(attempts):
            try:
                plan_attempt = await self._planner.plan_attempt(
                    state.user_request,
                    attempt=attempt,
                    feedback=feedback,
                )
                state.replan_count = attempt
                state.planning_attempts.append(plan_attempt)
                state.touch()
                await self._store.save_task(state)
                if plan_attempt.plan is not None:
                    return plan_attempt.plan
                last_error = plan_attempt.error
                log.warning(
                    "[%s] Plan invalid (attempt %d): %s",
                    state.task_id,
                    attempt,
                    plan_attempt.error,
                )
            except (PlanValidationError, ValueError) as exc:
                last_error = str(exc)
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
