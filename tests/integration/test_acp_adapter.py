"""ACP adapter (M0): drive the agent half over an in-memory connection.

No subprocess, no MLX — a fake orchestrator publishes the same typed EventBus
events the real one would, and we assert the adapter translates them into the
expected ``session/update`` stream and stop reason. This is the "scripted
JSON-RPC" validation from the milestone, run in-process for CI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from dagagent.config import get_settings
from dagagent.core import (
    NodeResult,
    NodeStatus,
    NodeType,
    TaskId,
    TaskStatus,
)
from dagagent.events import (
    EventBus,
    NodeFinished,
    NodeStarted,
    PlanReady,
    TaskCompleted,
)
from dagagent.executor import Orchestrator
from dagagent.gateways.acp.protocol import Connection, JsonRpcError
from dagagent.gateways.acp.server import AcpServer
from dagagent.harness import ToolHarness
from dagagent.runtime import Runtime
from dagagent.state import InMemoryStateStore

_PLAN: dict[str, Any] = {
    "nodes": [
        {"id": 1, "type": "tool", "description": "read the config", "tool": "file_read"},
        {"id": 2, "type": "result", "description": "answer the question"},
    ],
    "sorted_node_ids": [1, 2],
}


class _RecordingConnection(Connection):
    """A Connection that records outbound traffic instead of writing to a pipe."""

    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self._queued = incoming
        self.responses: list[tuple[Any, Any]] = []
        self.errors: list[tuple[Any, int, str]] = []
        self.notifications: list[dict[str, Any]] = []

    async def incoming(self) -> AsyncIterator[dict[str, Any]]:
        for msg in self._queued:
            yield msg

    async def respond(self, request_id: Any, result: Any) -> None:
        self.responses.append((request_id, result))

    async def respond_error(
        self, request_id: Any, code: int, message: str, data: Any | None = None
    ) -> None:
        self.errors.append((request_id, code, message))

    async def notify(self, method: str, params: Any) -> None:
        assert method == "session/update"
        self.notifications.append(params["update"])

    def updates_of(self, kind: str) -> list[dict[str, Any]]:
        return [u for u in self.notifications if u.get("sessionUpdate") == kind]


class _FakeOrchestrator(Orchestrator):
    """Publishes a canned event sequence for one task; no real planning."""

    def __init__(self, *, event_bus: EventBus, store: InMemoryStateStore) -> None:
        self._bus = event_bus
        self._store = store

    async def run(self, task_id: TaskId) -> None:  # type: ignore[override]
        state = await self._store.load_task(task_id)
        assert state is not None
        await self._bus.publish(PlanReady(task_id=task_id, node_count=2, plan=_PLAN))

        await self._bus.publish(
            NodeStarted(task_id=task_id, node_id=1, node_type=NodeType.TOOL, tier=0)
        )
        state.results[1] = NodeResult(
            node_id=1, status=NodeStatus.COMPLETED, output="port=8443", tier_used=0
        )
        await self._store.save_task(state)
        await self._bus.publish(
            NodeFinished(
                task_id=task_id,
                node_id=1,
                status=NodeStatus.COMPLETED,
                confidence=1.0,
                flagged=False,
                tier_used=0,
            )
        )

        await self._bus.publish(
            NodeStarted(task_id=task_id, node_id=2, node_type=NodeType.RESULT, tier=0)
        )
        state.results[2] = NodeResult(
            node_id=2, status=NodeStatus.COMPLETED, output="The port is 8443.", tier_used=0
        )
        state.final_output = "The port is 8443."
        state.status = TaskStatus.COMPLETED
        await self._store.save_task(state)
        await self._bus.publish(
            NodeFinished(
                task_id=task_id,
                node_id=2,
                status=NodeStatus.COMPLETED,
                confidence=1.0,
                flagged=False,
                tier_used=0,
            )
        )
        await self._bus.publish(TaskCompleted(task_id=task_id, final_output="The port is 8443."))


def _build_server(conn: Connection) -> AcpServer:
    bus = EventBus()
    store = InMemoryStateStore()
    orchestrator = _FakeOrchestrator(event_bus=bus, store=store)
    runtime = Runtime(
        orchestrator=orchestrator,
        store=store,
        harness=ToolHarness(),
        event_bus=bus,
        settings=get_settings(),
    )
    return AcpServer(runtime, conn)


async def test_initialize_advertises_protocol_and_capabilities() -> None:
    conn = _RecordingConnection([])
    server = _build_server(conn)
    result = await server.dispatch("initialize", {})
    assert result["protocolVersion"] == 1
    caps = result["agentCapabilities"]
    assert caps["loadSession"] is False
    assert "promptCapabilities" in caps


async def test_session_new_returns_session_id() -> None:
    conn = _RecordingConnection([])
    server = _build_server(conn)
    sid = (await server.dispatch("session/new", {}))["sessionId"]
    assert isinstance(sid, str) and sid


async def test_unknown_method_raises() -> None:
    conn = _RecordingConnection([])
    server = _build_server(conn)
    with pytest.raises(JsonRpcError):
        await server.dispatch("session/bogus", {})


async def test_prompt_streams_plan_nodes_and_result() -> None:
    conn = _RecordingConnection([])
    server = _build_server(conn)
    sid = (await server.dispatch("session/new", {}))["sessionId"]

    result = await server.dispatch(
        "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "what port?"}]}
    )

    assert result["stopReason"] == "end_turn"

    # Plan was emitted and its final render has every entry completed.
    plans = conn.updates_of("plan")
    assert plans, "expected at least one plan update"
    assert all(e["status"] == "completed" for e in plans[-1]["entries"])

    # The tool node produced a tool_call and a completed tool_call_update.
    assert conn.updates_of("tool_call"), "expected a tool_call for the tool node"
    tool_done = conn.updates_of("tool_call_update")
    assert tool_done and tool_done[-1]["status"] == "completed"

    # The final answer is streamed exactly once (via TaskCompleted, not the
    # result node) — no duplication.
    messages = [
        m["content"]["text"]
        for m in conn.updates_of("agent_message_chunk")
        if m["content"]["text"] == "The port is 8443."
    ]
    assert messages == ["The port is 8443."]


async def test_prompt_rejects_unknown_session() -> None:
    conn = _RecordingConnection([])
    server = _build_server(conn)
    with pytest.raises(JsonRpcError):
        await server.dispatch(
            "session/prompt", {"sessionId": "nope", "prompt": [{"type": "text", "text": "x"}]}
        )


async def test_prompt_rejects_empty_prompt() -> None:
    conn = _RecordingConnection([])
    server = _build_server(conn)
    sid = (await server.dispatch("session/new", {}))["sessionId"]
    with pytest.raises(JsonRpcError):
        await server.dispatch("session/prompt", {"sessionId": sid, "prompt": []})
