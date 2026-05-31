"""FastAPI surface.

``create_app`` builds a FastAPI instance from explicit dependencies — used
by tests to inject scripted providers and in-memory stores. ``app`` is the
module-level default for ``uvicorn dagagent.gateways.rest:app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from dagagent import __version__
from dagagent.config import Settings
from dagagent.core import ExecutionState, TaskId, TaskStatus, new_task_id
from dagagent.events import (
    TERMINAL_EVENT_TYPES,
    AwaitingConfirmation,
    Event,
    EventBus,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
)
from dagagent.executor import Orchestrator
from dagagent.harness import ToolHarness
from dagagent.runtime import build_runtime
from dagagent.state import StateStore


class TaskRequest(BaseModel):
    """Body of ``POST /task``."""

    request: str = Field(..., description="Natural language task description")


def _sse(event: Event) -> str:
    """Render one event as a Server-Sent Events ``data:`` frame."""
    return f"data: {event.model_dump_json()}\n\n"


def _terminal_snapshot(state: ExecutionState) -> Event | None:
    """The event that represents an already-finished task's current state.

    A client that connects after the task has already reached a terminal
    state would otherwise wait forever for events that have come and gone;
    we hand it one synthesised event and close the stream.
    """
    if state.status is TaskStatus.COMPLETED:
        return TaskCompleted(task_id=state.task_id, final_output=state.final_output)
    if state.status is TaskStatus.FAILED:
        if state.plan is None and state.planning_attempts:
            return TaskFailed(
                task_id=state.task_id,
                error=state.error or "task failed",
                attempts=[a.model_dump(mode="json") for a in state.planning_attempts],
            )
        return TaskFailed(task_id=state.task_id, error=state.error or "task failed")
    if state.status is TaskStatus.AWAITING_CONFIRMATION:
        node_count = len(state.plan.nodes) if state.plan else 0
        return AwaitingConfirmation(task_id=state.task_id, node_count=node_count)
    if state.status is TaskStatus.CANCELLED:
        return TaskCancelled(task_id=state.task_id)
    return None


async def _event_source(
    store: StateStore,
    bus: EventBus,
    task_id: TaskId,
) -> AsyncIterator[Event]:
    """Yield typed events for ``task_id`` until a terminal event.

    Transport-agnostic: SSE and WebSocket both consume this. If the task has
    already finished, it yields a single synthesised snapshot and stops rather
    than blocking on events that have already fired.
    """
    async with bus.subscribe() as sub:
        state = await store.load_task(task_id)
        if state is not None:
            snapshot = _terminal_snapshot(state)
            if snapshot is not None:
                yield snapshot
                return
        async for event in sub:
            if event.task_id != task_id:
                continue
            yield event
            if event.type in TERMINAL_EVENT_TYPES:
                return


async def _event_stream(
    store: StateStore,
    bus: EventBus,
    task_id: TaskId,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``task_id`` until a terminal event."""
    async for event in _event_source(store, bus, task_id):
        yield _sse(event)


def create_app(
    *,
    orchestrator: Orchestrator,
    store: StateStore,
    harness: ToolHarness,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Build the FastAPI app from explicit collaborators."""
    api = FastAPI(title="dagagent", version=__version__)

    @api.post("/task", status_code=202)
    async def submit_task(
        body: TaskRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        state = ExecutionState(task_id=new_task_id(), user_request=body.request)
        await store.save_task(state)
        background_tasks.add_task(orchestrator.run, state.task_id)
        return {"task_id": state.task_id, "status": state.status.value}

    @api.get("/task/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        state = await store.load_task(TaskId(task_id))
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return state.model_dump(mode="json")

    @api.post("/task/{task_id}/resume")
    async def resume_task(
        task_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        state = await store.load_task(TaskId(task_id))
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if state.status not in (TaskStatus.AWAITING_CONFIRMATION, TaskStatus.PAUSED):
            raise HTTPException(
                status_code=400,
                detail=f"Task is in status '{state.status.value}', not resumable",
            )
        background_tasks.add_task(orchestrator.resume, TaskId(task_id))
        return {"task_id": task_id, "status": "resuming"}

    @api.get("/task/{task_id}/branch-log")
    async def get_branch_log(task_id: str) -> dict[str, Any]:
        state = await store.load_task(TaskId(task_id))
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return {
            "task_id": task_id,
            "branch_log": [e.model_dump(mode="json") for e in state.branch_log],
            "flagged_nodes": [
                {"node_id": r.node_id, "confidence": r.confidence, "error": r.error}
                for r in state.results.values()
                if r.flagged
            ],
        }

    @api.get("/task/{task_id}/events")
    async def task_events(task_id: str) -> StreamingResponse:
        if event_bus is None:
            raise HTTPException(status_code=503, detail="Event stream not configured")
        tid = TaskId(task_id)
        if await store.load_task(tid) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return StreamingResponse(
            _event_stream(store, event_bus, tid),
            media_type="text/event-stream",
        )

    @api.websocket("/task/{task_id}/events")
    async def task_events_ws(websocket: WebSocket, task_id: str) -> None:
        # Reject before accepting so the client sees a close code, not an open
        # socket that immediately drops. 1011 = internal error, 1008 = policy.
        if event_bus is None:
            await websocket.close(code=1011, reason="Event stream not configured")
            return
        tid = TaskId(task_id)
        if await store.load_task(tid) is None:
            await websocket.close(code=1008, reason="Task not found")
            return

        await websocket.accept()
        try:
            async for event in _event_source(store, event_bus, tid):
                await websocket.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            return  # client hung up mid-stream; nothing to clean up
        await websocket.close()

    @api.get("/tools")
    async def list_tools() -> dict[str, Any]:
        return {"tools": harness.all_schemas()}

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    return api


def create_default_app(settings: Settings | None = None) -> FastAPI:
    """Build the app with the default in-memory wiring from :class:`Settings`."""
    rt = build_runtime(settings)
    return create_app(
        orchestrator=rt.orchestrator,
        store=rt.store,
        harness=rt.harness,
        event_bus=rt.event_bus,
    )


# Module-level app for `uvicorn dagagent.gateways.rest:app`.
app = create_default_app()
