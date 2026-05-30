"""Integration tests for the FastAPI gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

import pytest
from fastapi.testclient import TestClient

from dagagent.config import Settings
from dagagent.core import ExecutionState, NodeResult, NodeStatus, TaskStatus, new_task_id
from dagagent.executor import Executor, Orchestrator
from dagagent.gateways.rest import create_app
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


@pytest.fixture
def wired() -> tuple[TestClient, InMemoryStateStore]:
    settings = Settings()
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_Scripted([])])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator, settings=settings)
    executor = Executor(router=router, harness=harness, settings=settings)
    store = InMemoryStateStore()
    orch = Orchestrator(planner=planner, executor=executor, state_store=store, settings=settings)
    app = create_app(orchestrator=orch, store=store, harness=harness)
    return TestClient(app), store


def test_health(wired: tuple[TestClient, InMemoryStateStore]) -> None:
    client, _ = wired
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_tools_lists_builtins(wired: tuple[TestClient, InMemoryStateStore]) -> None:
    client, _ = wired
    resp = client.get("/tools")
    assert resp.status_code == 200
    names = {t["function"]["name"] for t in resp.json()["tools"]}
    assert names == {"calendar_get", "memory_search", "file_read"}


def test_submit_task_returns_id_and_status(
    wired: tuple[TestClient, InMemoryStateStore],
) -> None:
    client, _ = wired
    resp = client.post("/task", json={"request": "do a thing"})
    assert resp.status_code == 202
    body = resp.json()
    assert "task_id" in body
    assert body["status"] in {s.value for s in TaskStatus}
    # The orchestrator runs as a background task — verify the row exists.
    # (Whether it completes depends on the scripted provider.)
    assert len(body["task_id"]) == 26  # ULID


async def test_get_task_returns_state(
    wired: tuple[TestClient, InMemoryStateStore],
) -> None:
    client, store = wired
    state = ExecutionState(task_id=new_task_id(), user_request="hi")
    await store.save_task(state)

    resp = client.get(f"/task/{state.task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == state.task_id
    assert body["user_request"] == "hi"
    assert body["status"] == TaskStatus.PLANNING.value


def test_get_task_missing_returns_404(
    wired: tuple[TestClient, InMemoryStateStore],
) -> None:
    client, _ = wired
    resp = client.get(f"/task/{new_task_id()}")
    assert resp.status_code == 404


async def test_resume_rejects_non_paused(
    wired: tuple[TestClient, InMemoryStateStore],
) -> None:
    client, store = wired
    state = ExecutionState(task_id=new_task_id(), user_request="x")
    await store.save_task(state)

    resp = client.post(f"/task/{state.task_id}/resume")
    assert resp.status_code == 400


async def test_resume_missing_returns_404(
    wired: tuple[TestClient, InMemoryStateStore],
) -> None:
    client, _ = wired
    resp = client.post(f"/task/{new_task_id()}/resume")
    assert resp.status_code == 404


async def test_branch_log_endpoint(
    wired: tuple[TestClient, InMemoryStateStore],
) -> None:
    client, store = wired
    state = ExecutionState(task_id=new_task_id(), user_request="x")
    # Add a flagged result so the endpoint surfaces something.
    state.results[1] = NodeResult(
        node_id=1, status=NodeStatus.COMPLETED, confidence=0.3, flagged=True, error="low conf"
    )
    await store.save_task(state)

    resp = client.get(f"/task/{state.task_id}/branch-log")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == state.task_id
    assert body["flagged_nodes"] == [
        {"node_id": 1, "confidence": 0.3, "error": "low conf"},
    ]
