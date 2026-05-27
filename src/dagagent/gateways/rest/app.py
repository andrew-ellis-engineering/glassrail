"""FastAPI surface.

``create_app`` builds a FastAPI instance from explicit dependencies — used
by tests to inject scripted providers and in-memory stores. ``app`` is the
module-level default for ``uvicorn dagagent.gateways.rest:app``.
"""

from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from dagagent import __version__
from dagagent.config import Settings, get_settings
from dagagent.core import ExecutionState, TaskId, TaskStatus, new_task_id
from dagagent.executor import Executor, Orchestrator
from dagagent.harness import ToolHarness, register_builtins
from dagagent.planner import Planner
from dagagent.providers import router_from_settings
from dagagent.state import InMemoryStateStore, StateStore
from dagagent.validator import PlanValidator


class TaskRequest(BaseModel):
    """Body of ``POST /task``."""

    request: str = Field(..., description="Natural language task description")


def create_app(
    *,
    orchestrator: Orchestrator,
    store: StateStore,
    harness: ToolHarness,
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

    @api.get("/tools")
    async def list_tools() -> dict[str, Any]:
        return {"tools": harness.all_schemas()}

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    return api


def create_default_app(settings: Settings | None = None) -> FastAPI:
    """Build the app with the default in-memory wiring from :class:`Settings`."""
    settings = settings or get_settings()

    harness = ToolHarness()
    register_builtins(harness)
    router = router_from_settings(settings)
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator)
    executor = Executor(router=router, harness=harness, settings=settings)
    store = InMemoryStateStore()
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
    )
    return create_app(orchestrator=orchestrator, store=store, harness=harness)


# Module-level app for `uvicorn dagagent.gateways.rest:app`.
app = create_default_app()
