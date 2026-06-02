"""ACP adapter (M0): drive the agent half over an in-memory connection.

No subprocess, no MLX — a fake orchestrator publishes the same typed EventBus
events the real one would, and we assert the adapter translates them into the
expected ``session/update`` stream and stop reason. This is the "scripted
JSON-RPC" validation from the milestone, run in-process for CI.
"""

from __future__ import annotations

import asyncio
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
    AwaitingConfirmation,
    EventBus,
    NodeFinished,
    NodeOutputChunk,
    NodeStarted,
    PlanReady,
    TaskCompleted,
)
from dagagent.executor import Orchestrator
from dagagent.executor.tool_approval import ToolApprovalBroker
from dagagent.gateways.acp.protocol import Connection, JsonRpcError
from dagagent.gateways.acp.server import AcpServer
from dagagent.gateways.acp.session import Session
from dagagent.harness import ToolHarness
from dagagent.runtime import Runtime
from dagagent.state import InMemoryStateStore

_PLAN: dict[str, Any] = {
    "nodes": [
        {"id": 1, "type": "tool", "description": "read the config", "tool": "file_read"},
        {"id": 3, "type": "think", "description": "inspect the tool result", "context_needed": [1]},
        {"id": 2, "type": "result", "description": "answer the question", "context_needed": [1, 3]},
    ],
    "sorted_node_ids": [1, 3, 2],
}


class _RecordingConnection(Connection):
    """A Connection that records outbound traffic instead of writing to a pipe."""

    def __init__(
        self,
        incoming: list[dict[str, Any]],
        *,
        permissions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._queued = incoming
        self._permissions = list(permissions or [])
        self.responses: list[tuple[Any, Any]] = []
        self.errors: list[tuple[Any, int, str]] = []
        self.notifications: list[dict[str, Any]] = []
        self.requests: list[tuple[str, Any]] = []

    async def incoming(self) -> AsyncIterator[dict[str, Any]]:
        for msg in self._queued:
            yield msg

    async def request(self, method: str, params: Any) -> Any:
        """Answer an agent→client request from the scripted queue."""
        self.requests.append((method, params))
        if not self._permissions:
            raise AssertionError(f"unexpected outbound request: {method}")
        return self._permissions.pop(0)

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
        self.seen_requests: list[str] = []

    async def run(self, task_id: TaskId) -> None:  # type: ignore[override]
        state = await self._store.load_task(task_id)
        assert state is not None
        self.seen_requests.append(state.user_request)
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
            NodeStarted(task_id=task_id, node_id=3, node_type=NodeType.THINK, tier=2)
        )
        state.results[3] = NodeResult(
            node_id=3, status=NodeStatus.COMPLETED, output="I checked the port.", tier_used=2
        )
        await self._store.save_task(state)
        await self._bus.publish(
            NodeOutputChunk(
                task_id=task_id,
                node_id=3,
                node_type=NodeType.THINK,
                text="I checked the port.",
            )
        )
        await self._bus.publish(
            NodeFinished(
                task_id=task_id,
                node_id=3,
                status=NodeStatus.COMPLETED,
                confidence=0.8,
                flagged=False,
                tier_used=2,
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


class _GatingOrchestrator(Orchestrator):
    """Pauses at a plan gate, then completes on resume or re-gates on revise."""

    def __init__(self, *, event_bus: EventBus, store: InMemoryStateStore) -> None:
        self._bus = event_bus
        self._store = store
        self.revise_feedback: list[str] = []

    async def _present(self, task_id: TaskId, description: str) -> None:
        plan = {
            "nodes": [{"id": 1, "type": "result", "description": description}],
            "sorted_node_ids": [1],
        }
        state = await self._store.load_task(task_id)
        assert state is not None
        state.status = TaskStatus.AWAITING_CONFIRMATION
        await self._store.save_task(state)
        await self._bus.publish(PlanReady(task_id=task_id, node_count=1, plan=plan))
        await self._bus.publish(AwaitingConfirmation(task_id=task_id, node_count=1))

    async def run(self, task_id: TaskId) -> None:  # type: ignore[override]
        await self._present(task_id, "draft the answer")

    async def revise(self, task_id: TaskId, feedback: str) -> None:  # type: ignore[override]
        self.revise_feedback.append(feedback)
        await self._present(task_id, "revised: draft the answer")

    async def resume(self, task_id: TaskId) -> None:  # type: ignore[override]
        state = await self._store.load_task(task_id)
        assert state is not None
        await self._bus.publish(
            NodeStarted(task_id=task_id, node_id=1, node_type=NodeType.RESULT, tier=0)
        )
        state.results[1] = NodeResult(
            node_id=1, status=NodeStatus.COMPLETED, output="42.", tier_used=0
        )
        state.final_output = "42."
        state.status = TaskStatus.COMPLETED
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
        await self._bus.publish(TaskCompleted(task_id=task_id, final_output="42."))


def _build_gating_server(conn: Connection) -> tuple[AcpServer, _GatingOrchestrator]:
    bus = EventBus()
    store = InMemoryStateStore()
    orchestrator = _GatingOrchestrator(event_bus=bus, store=store)
    runtime = Runtime(
        orchestrator=orchestrator,
        store=store,
        harness=ToolHarness(),
        event_bus=bus,
        settings=get_settings(),
    )
    return AcpServer(runtime, conn), orchestrator


_APPROVE: dict[str, Any] = {"outcome": {"outcome": "selected", "optionId": "approve"}}
_REJECT: dict[str, Any] = {"outcome": {"outcome": "selected", "optionId": "reject"}}
_ALLOW_ONCE: dict[str, Any] = {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
_ALWAYS_ALLOW: dict[str, Any] = {"outcome": {"outcome": "selected", "optionId": "always_allow"}}
_CANCEL: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}


def _reject_with(feedback: str) -> dict[str, Any]:
    return {"outcome": {"outcome": "selected", "optionId": "reject"}, "feedback": feedback}


async def _prompt(server: AcpServer, conn: _RecordingConnection) -> dict[str, Any]:
    sid = (await server.dispatch("session/new", {}))["sessionId"]
    return await server.dispatch(
        "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]}
    )


async def test_gate_approve_runs_to_completion() -> None:
    conn = _RecordingConnection([], permissions=[_APPROVE])
    server, _ = _build_gating_server(conn)

    result = await _prompt(server, conn)

    assert result["stopReason"] == "end_turn"
    assert [m for m, _ in conn.requests] == ["session/request_permission"]
    perm = conn.requests[0][1]
    assert {o["optionId"] for o in perm["options"]} == {"approve", "reject"}
    assert any(u["content"]["text"] == "42." for u in conn.updates_of("agent_message_chunk"))


async def test_gate_reject_with_feedback_revises_then_approves() -> None:
    conn = _RecordingConnection([], permissions=[_reject_with("make it shorter"), _APPROVE])
    server, orch = _build_gating_server(conn)

    result = await _prompt(server, conn)

    assert result["stopReason"] == "end_turn"
    assert orch.revise_feedback == ["make it shorter"]
    # Two gates: the initial plan and the revised plan.
    assert [m for m, _ in conn.requests] == [
        "session/request_permission",
        "session/request_permission",
    ]
    plans = conn.updates_of("plan")
    assert any("revised" in e["content"] for p in plans for e in p["entries"])


async def test_gate_reject_without_feedback_refuses() -> None:
    conn = _RecordingConnection([], permissions=[_REJECT])
    server, orch = _build_gating_server(conn)

    result = await _prompt(server, conn)

    assert result["stopReason"] == "refusal"
    assert orch.revise_feedback == []


async def test_gate_cancel_outcome_cancels_turn() -> None:
    conn = _RecordingConnection([], permissions=[_CANCEL])
    server, _ = _build_gating_server(conn)

    result = await _prompt(server, conn)

    assert result["stopReason"] == "cancelled"


class _ToolApprovalOrchestrator(Orchestrator):
    def __init__(self, *, broker: ToolApprovalBroker, event_bus: EventBus) -> None:
        self._broker = broker
        self._bus = event_bus
        self.approvals: list[bool] = []

    async def run(self, task_id: TaskId) -> None:  # type: ignore[override]
        approved = await self._broker.request(
            task_id=task_id,
            node_id=7,
            tool_name="file_write",
            risk="write",
            args={"path": "/tmp/out.txt", "content": "hello"},
            description="write a file",
        )
        self.approvals.append(approved)
        await self._bus.publish(
            TaskCompleted(task_id=task_id, final_output="approved" if approved else "denied")
        )


def _build_tool_approval_server(
    conn: Connection,
) -> tuple[AcpServer, _ToolApprovalOrchestrator, ToolApprovalBroker]:
    bus = EventBus()
    store = InMemoryStateStore()
    broker = ToolApprovalBroker(bus)
    orchestrator = _ToolApprovalOrchestrator(broker=broker, event_bus=bus)
    runtime = Runtime(
        orchestrator=orchestrator,
        store=store,
        harness=ToolHarness(),
        event_bus=bus,
        settings=get_settings(),
        tool_approval=broker,
    )
    return AcpServer(runtime, conn), orchestrator, broker


async def test_tool_approval_request_allows_once() -> None:
    conn = _RecordingConnection([], permissions=[_ALLOW_ONCE])
    server, orch, _ = _build_tool_approval_server(conn)

    result = await _prompt(server, conn)

    assert result["stopReason"] == "end_turn"
    assert orch.approvals == [True]
    method, params = conn.requests[0]
    assert method == "session/request_permission"
    assert params["kind"] == "tool_call"
    assert params["toolCall"]["toolName"] == "file_write"
    assert params["toolCall"]["risk"] == "write"
    assert {o["optionId"] for o in params["options"]} == {
        "allow_once",
        "always_allow",
        "deny",
    }


async def test_tool_approval_always_allow_is_remembered() -> None:
    conn = _RecordingConnection([], permissions=[_ALWAYS_ALLOW])
    server, orch, broker = _build_tool_approval_server(conn)

    await _prompt(server, conn)

    assert orch.approvals == [True]
    assert broker.is_always_allowed("file_write")


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
    assert [entry["nodeId"] for entry in plans[0]["entries"]] == [1, 3, 2]
    assert all(e["status"] == "completed" for e in plans[-1]["entries"])

    # The tool node produced a tool_call and a completed tool_call_update.
    tool_calls = conn.updates_of("tool_call")
    assert tool_calls, "expected a tool_call for the tool node"
    assert "rawInput" in tool_calls[0]
    tool_done = conn.updates_of("tool_call_update")
    assert tool_done and tool_done[-1]["status"] == "completed"
    assert tool_done[-1]["rawOutput"]["output"] == "port=8443"

    # Per-node tier/confidence is streamed as the node_meta extension.
    metas = conn.updates_of("node_meta")
    assert metas, "expected node_meta updates"
    assert all("tier" in m and "confidence" in m for m in metas)
    assert {m["nodeType"] for m in metas} == {"tool", "think", "result"}

    # The graph topology is streamed once as the plan_graph extension.
    graphs = conn.updates_of("plan_graph")
    assert len(graphs) == 1
    nodes = graphs[0]["nodes"]
    assert [n["id"] for n in nodes] == [1, 3, 2]
    assert {n["nodeType"] for n in nodes} == {"tool", "think", "result"}
    assert all("deps" in n for n in nodes)
    assert graphs[0]["edges"] == [
        {"from": 1, "to": 3, "kind": "data"},
        {"from": 1, "to": 2, "kind": "data"},
        {"from": 3, "to": 2, "kind": "data"},
    ]

    # The final answer is streamed exactly once (via TaskCompleted, not the
    # result node) — no duplication.
    messages = [
        m
        for m in conn.updates_of("agent_message_chunk")
        if m["content"]["text"] == "The port is 8443."
    ]
    assert len(messages) == 1
    assert messages[0]["nodeType"] == "result"
    assert messages[0]["isFinal"] is True

    intermediate = [
        m
        for m in conn.updates_of("agent_message_chunk")
        if m["content"]["text"] == "I checked the port."
    ]
    assert len(intermediate) == 1
    assert intermediate[0]["nodeId"] == 3
    assert intermediate[0]["nodeType"] == "think"
    assert intermediate[0]["isFinal"] is False


class _BranchGraphOrchestrator(Orchestrator):
    def __init__(self, *, event_bus: EventBus) -> None:
        self._bus = event_bus

    async def run(self, task_id: TaskId) -> None:  # type: ignore[override]
        await self._bus.publish(
            PlanReady(
                task_id=task_id,
                node_count=3,
                plan={
                    "nodes": [
                        {
                            "id": 1,
                            "type": "decision",
                            "description": "choose path",
                            "branches": {"yes": [2], "no": [3]},
                        },
                        {"id": 2, "type": "tool", "description": "yes path"},
                        {"id": 3, "type": "result", "description": "no path"},
                    ],
                    "sorted_node_ids": [1, 2, 3],
                },
            )
        )
        await self._bus.publish(TaskCompleted(task_id=task_id, final_output="done"))


async def test_plan_graph_includes_decision_control_edges() -> None:
    conn = _RecordingConnection([])
    bus = EventBus()
    runtime = Runtime(
        orchestrator=_BranchGraphOrchestrator(event_bus=bus),
        store=InMemoryStateStore(),
        harness=ToolHarness(),
        event_bus=bus,
        settings=get_settings(),
    )
    server = AcpServer(runtime, conn)

    await _prompt(server, conn)

    graphs = conn.updates_of("plan_graph")
    assert len(graphs) == 1
    assert graphs[0]["edges"] == [
        {"from": 1, "to": 2, "kind": "control", "label": "yes"},
        {"from": 1, "to": 3, "kind": "control", "label": "no"},
    ]


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


async def test_dovetail_threads_prior_result_into_follow_up() -> None:
    conn = _RecordingConnection([])
    bus = EventBus()
    store = InMemoryStateStore()
    orch = _FakeOrchestrator(event_bus=bus, store=store)
    runtime = Runtime(
        orchestrator=orch,
        store=store,
        harness=ToolHarness(),
        event_bus=bus,
        settings=get_settings(),
    )
    server = AcpServer(runtime, conn)
    sid = (await server.dispatch("session/new", {}))["sessionId"]

    await server.dispatch(
        "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "what port?"}]}
    )
    await server.dispatch(
        "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "and the host?"}]}
    )

    assert len(orch.seen_requests) == 2
    # The first task is verbatim; the follow-up carries the prior result.
    assert orch.seen_requests[0] == "what port?"
    follow_up = orch.seen_requests[1]
    assert "Context from the previous step:" in follow_up
    assert "The port is 8443." in follow_up
    assert "New request: and the host?" in follow_up


class _BlockingOrchestrator(Orchestrator):
    """Never emits anything; blocks until its run task is cancelled."""

    def __init__(self) -> None:
        pass

    async def run(self, task_id: TaskId) -> None:  # type: ignore[override]
        await asyncio.Event().wait()


async def test_session_cancel_ends_turn_as_cancelled() -> None:
    conn = _RecordingConnection([])
    bus = EventBus()
    store = InMemoryStateStore()
    runtime = Runtime(
        orchestrator=_BlockingOrchestrator(),
        store=store,
        harness=ToolHarness(),
        event_bus=bus,
        settings=get_settings(),
    )
    server = AcpServer(runtime, conn)
    sid = (await server.dispatch("session/new", {}))["sessionId"]

    turn = asyncio.create_task(
        server.dispatch(
            "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]}
        )
    )
    # Give the turn time to register its cancel signal, then cancel it.
    await asyncio.sleep(0.02)
    await server.handle_notification("session/cancel", {"sessionId": sid})

    result = await asyncio.wait_for(turn, timeout=2)
    assert result["stopReason"] == "cancelled"


def test_session_compose_request_is_verbatim_without_context() -> None:
    session = Session(id="s")
    assert session.compose_request("do the thing") == "do the thing"


def test_session_compose_request_threads_carried_context() -> None:
    session = Session(id="s", carried_context="prior answer")
    composed = session.compose_request("next")
    assert composed.startswith("Context from the previous step:\nprior answer")
    assert composed.endswith("New request: next")
