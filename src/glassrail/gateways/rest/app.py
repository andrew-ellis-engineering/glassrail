"""FastAPI surface.

``create_app`` builds a FastAPI instance from explicit dependencies — used
by tests to inject scripted providers and in-memory stores. ``app`` is the
module-level default for ``uvicorn glassrail.gateways.rest:app``.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from glassrail import __version__
from glassrail.config import Settings
from glassrail.core import ExecutionState, TaskId, TaskStatus, new_task_id
from glassrail.events import (
    TERMINAL_EVENT_TYPES,
    AwaitingConfirmation,
    Event,
    EventBus,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
)
from glassrail.executor import Orchestrator
from glassrail.harness import ToolHarness
from glassrail.runtime import build_runtime
from glassrail.state import StateStore


class TaskRequest(BaseModel):
    """Body of ``POST /task``."""

    request: str = Field(..., description="Natural language task description")


@dataclass
class _RestRuntime:
    orchestrator: Orchestrator
    store: StateStore
    harness: ToolHarness
    event_bus: EventBus | None
    settings: Settings
    api_key: str | None = None
    close: Callable[[], Awaitable[None]] | None = None

    async def aclose(self) -> None:
        if self.close is not None:
            await self.close()


def _runtime_from_app(api: FastAPI) -> _RestRuntime:
    runtime = getattr(api.state, "runtime", None)
    if not isinstance(runtime, _RestRuntime):
        raise RuntimeError("Glassrail runtime has not been initialised")
    return runtime


def _wrap_runtime(settings: Settings) -> _RestRuntime:
    rt = build_runtime(settings)
    return _RestRuntime(
        orchestrator=rt.orchestrator,
        store=rt.store,
        harness=rt.harness,
        event_bus=rt.event_bus,
        settings=rt.settings,
        api_key=rt.settings.api_key,
        close=rt.aclose,
    )


def _sse(event: Event) -> str:
    """Render one event as a Server-Sent Events ``data:`` frame."""
    return f"data: {event.model_dump_json()}\n\n"


def _authorized_header(value: str | None, api_key: str | None) -> bool:
    if api_key is None:
        return True
    if value is None or not value.startswith("Bearer "):
        return False
    token = value.removeprefix("Bearer ").strip()
    return secrets.compare_digest(token, api_key)


def _authorized_ws(websocket: WebSocket, api_key: str | None) -> bool:
    if api_key is None:
        return True
    if _authorized_header(websocket.headers.get("authorization"), api_key):
        return True
    query_key = websocket.query_params.get("api_key")
    return query_key is not None and secrets.compare_digest(query_key, api_key)


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


def _lifespan_for(
    settings: Settings | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncGenerator[None]:
        if not hasattr(api.state, "runtime"):
            api.state.runtime = _wrap_runtime(settings or Settings())
        try:
            yield
        finally:
            await _runtime_from_app(api).aclose()

    return lifespan


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


def _install_auth_middleware(api: FastAPI) -> None:
    @api.middleware("http")
    async def require_bearer(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path == "/health":
            return await call_next(request)
        runtime = _runtime_from_app(request.app)
        if not _authorized_header(request.headers.get("authorization"), runtime.api_key):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


def _install_task_routes(api: FastAPI) -> None:
    @api.post("/task", status_code=202)
    async def submit_task(
        body: TaskRequest,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> dict[str, Any]:
        runtime = _runtime_from_app(request.app)
        state = ExecutionState(task_id=new_task_id(), user_request=body.request)
        await runtime.store.save_task(state)
        background_tasks.add_task(runtime.orchestrator.run, state.task_id)
        return {"task_id": state.task_id, "status": state.status.value}

    @api.get("/task/{task_id}")
    async def get_task(task_id: str, request: Request) -> dict[str, Any]:
        runtime = _runtime_from_app(request.app)
        state = await runtime.store.load_task(TaskId(task_id))
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return state.model_dump(mode="json")

    @api.post("/task/{task_id}/resume")
    async def resume_task(
        task_id: str,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> dict[str, Any]:
        runtime = _runtime_from_app(request.app)
        state = await runtime.store.load_task(TaskId(task_id))
        if state is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if state.status not in (TaskStatus.AWAITING_CONFIRMATION, TaskStatus.PAUSED):
            raise HTTPException(
                status_code=400,
                detail=f"Task is in status '{state.status.value}', not resumable",
            )
        background_tasks.add_task(runtime.orchestrator.resume, TaskId(task_id))
        return {"task_id": task_id, "status": "resuming"}

    @api.get("/task/{task_id}/branch-log")
    async def get_branch_log(task_id: str, request: Request) -> dict[str, Any]:
        runtime = _runtime_from_app(request.app)
        state = await runtime.store.load_task(TaskId(task_id))
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


def _install_event_routes(api: FastAPI) -> None:
    @api.get("/task/{task_id}/events")
    async def task_events(task_id: str, request: Request) -> StreamingResponse:
        runtime = _runtime_from_app(request.app)
        if runtime.event_bus is None:
            raise HTTPException(status_code=503, detail="Event stream not configured")
        tid = TaskId(task_id)
        if await runtime.store.load_task(tid) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return StreamingResponse(
            _event_stream(runtime.store, runtime.event_bus, tid),
            media_type="text/event-stream",
        )

    @api.websocket("/task/{task_id}/events")
    async def task_events_ws(websocket: WebSocket, task_id: str) -> None:
        runtime = _runtime_from_app(websocket.app)
        # Reject before accepting so the client sees a close code, not an open
        # socket that immediately drops. 1011 = internal error, 1008 = policy.
        if not _authorized_ws(websocket, runtime.api_key):
            await websocket.close(code=1008, reason="Unauthorized")
            return
        if runtime.event_bus is None:
            await websocket.close(code=1011, reason="Event stream not configured")
            return
        tid = TaskId(task_id)
        if await runtime.store.load_task(tid) is None:
            await websocket.close(code=1008, reason="Task not found")
            return

        await websocket.accept()
        try:
            async for event in _event_source(runtime.store, runtime.event_bus, tid):
                await websocket.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            return  # client hung up mid-stream; nothing to clean up
        await websocket.close()


def _install_misc_routes(api: FastAPI) -> None:
    @api.get("/tools")
    async def list_tools(request: Request) -> dict[str, Any]:
        runtime = _runtime_from_app(request.app)
        return {"tools": runtime.harness.all_schemas()}

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}


def create_app(
    *,
    orchestrator: Orchestrator | None = None,
    store: StateStore | None = None,
    harness: ToolHarness | None = None,
    event_bus: EventBus | None = None,
    settings: Settings | None = None,
    api_key: str | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """Build the FastAPI app from explicit collaborators."""
    api = FastAPI(title="Glassrail", version=__version__, lifespan=_lifespan_for(settings))
    explicit_deps = (orchestrator, store, harness)
    if any(dep is not None for dep in explicit_deps):
        if orchestrator is None or store is None or harness is None:
            raise ValueError("orchestrator, store, and harness must be provided together")
        resolved_settings = settings or Settings()
        api.state.runtime = _RestRuntime(
            orchestrator=orchestrator,
            store=store,
            harness=harness,
            event_bus=event_bus,
            settings=resolved_settings,
            api_key=api_key if api_key is not None else resolved_settings.api_key,
            close=on_shutdown,
        )
    _install_auth_middleware(api)
    _install_task_routes(api)
    _install_event_routes(api)
    _install_misc_routes(api)
    return api


def create_default_app(settings: Settings | None = None) -> FastAPI:
    """Build the default app, deferring runtime wiring to ASGI lifespan startup."""
    return create_app(settings=settings)


# Module-level app for `uvicorn glassrail.gateways.rest:app`.
app = create_default_app()
