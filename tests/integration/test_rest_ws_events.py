"""Integration tests for the WebSocket events endpoint.

Mirrors the SSE coverage: the WebSocket carries the same typed events and
closes on a terminal event. As with the SSE tests, the background run finishes
before the POST returns under TestClient, so a client that connects afterwards
gets the synthesised terminal snapshot and then a clean close.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Sequence as _Sequence

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from dagagent.config import Settings
from dagagent.core import new_task_id
from dagagent.events import EventBus
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


_PLAN_PAYLOAD = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "tool",
                "description": "get today",
                "tool": "calendar_get",
                "args_template": {"date": "2026-05-27"},
                "context_needed": [],
            },
            {"id": 2, "type": "synthesis", "description": "summarise", "context_needed": [1]},
        ]
    }
)
_SHAPE_OK = json.dumps({"matches_expectation": True, "issue": None})
_SYNTH_OUT = json.dumps({"output": "nothing scheduled.", "confidence": 0.9})


def _wired(responses: list[str], *, with_bus: bool = True) -> TestClient:
    settings = Settings()
    bus = EventBus() if with_bus else None
    harness = ToolHarness()
    register_builtins(harness)
    router = TierRouter([_Scripted(responses)])
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator)
    executor = Executor(router=router, harness=harness, settings=settings, event_bus=bus)
    store = InMemoryStateStore()
    orch = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
        event_bus=bus,
    )
    app = create_app(orchestrator=orch, store=store, harness=harness, event_bus=bus)
    return TestClient(app)


def test_ws_streams_snapshot_then_closes_for_completed_task() -> None:
    client = _wired([_PLAN_PAYLOAD, _SHAPE_OK, _SYNTH_OUT])
    task_id = client.post("/task", json={"request": "what's today?"}).json()["task_id"]

    with client.websocket_connect(f"/task/{task_id}/events") as ws:
        event = json.loads(ws.receive_text())
        assert event["type"] == "task_completed"
        assert event["final_output"] == "nothing scheduled."
        assert event["task_id"] == task_id
        # Terminal event delivered -> the server closes the socket.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_ws_snapshot_for_failed_task() -> None:
    client = _wired(["not json", "still not json"])
    task_id = client.post("/task", json={"request": "do a thing"}).json()["task_id"]

    with client.websocket_connect(f"/task/{task_id}/events") as ws:
        event = json.loads(ws.receive_text())
        assert event["type"] == "task_failed"
        assert event["task_id"] == task_id


def test_ws_unknown_task_is_rejected() -> None:
    client = _wired([])
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/task/{new_task_id()}/events"):
            pass
    assert exc_info.value.code == 1008


def test_ws_without_bus_is_rejected() -> None:
    client = _wired([], with_bus=False)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/task/{new_task_id()}/events"):
            pass
    assert exc_info.value.code == 1011
