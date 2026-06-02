"""The ACP agent: dispatch JSON-RPC methods and bridge the EventBus.

Implements the agent half of ACP over a :class:`~.protocol.Connection`:
``initialize``, ``session/new``, ``session/prompt``, and the ``session/cancel``
notification. A prompt runs one task through the existing
:class:`~dagagent.executor.orchestrator.Orchestrator`; while it runs, typed
EventBus events are translated into ``session/update`` notifications, and the
turn ends by returning a stop reason.

Out of scope for this milestone (advertised as unsupported in ``initialize``):
``fs/*`` and ``terminal/*`` — dag-agent runs its own tools server-side, nothing
is delegated to the client — and ``session/load`` (sessions are in-memory).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, cast

from dagagent.core import ExecutionState, NodeStatus, TaskId, new_task_id
from dagagent.events import (
    AwaitingConfirmation,
    BranchDecided,
    Event,
    NodeFinished,
    NodeOutputChunk,
    NodeStarted,
    PlanFailed,
    PlanReady,
    PlanRejected,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
    ToolApprovalRequested,
)
from dagagent.gateways.acp.mapping import PlanTracker
from dagagent.gateways.acp.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    Connection,
    JsonRpcError,
)
from dagagent.gateways.acp.session import Session, SessionRegistry
from dagagent.runtime import Runtime

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# dag-agent tool name → ACP tool-call kind (best-effort; defaults to "other").
_TOOL_KINDS: dict[str, str] = {
    "file_read": "read",
    "file_write": "edit",
    "web_fetch": "fetch",
    "web_search": "search",
    "shell": "execute",
}

# Node types whose intermediate output is worth streaming as a message chunk.
# "result" is excluded: its text is the task's final_output, which TaskCompleted
# already streams — emitting both would duplicate the answer.
# Must mirror executor._STREAMING_NODE_TYPES (same three types, as strings here).
# If one changes, update the other; the test_acp_adapter tests guard this.
_MESSAGE_NODE_TYPES = frozenset({"think", "summary", "synthesis"})


class AcpServer:
    """Speaks ACP over one stdio connection, backed by one agent runtime."""

    def __init__(self, runtime: Runtime, conn: Connection) -> None:
        self._rt = runtime
        self._conn = conn
        self._sessions = SessionRegistry()
        self._cancels: dict[str, asyncio.Event] = {}

    async def serve(self) -> None:
        """Read messages until stdin closes, dispatching each."""
        tasks: set[asyncio.Task[None]] = set()
        async for msg in self._conn.incoming():
            method = msg.get("method")
            if method is None:
                continue  # stray response with no pending request
            if "id" in msg:
                t = asyncio.create_task(self._handle_request(msg))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            else:
                params: dict[str, Any] = msg.get("params") or {}
                await self.handle_notification(method, params)
        # stdin closed: let any in-flight request handlers finish before exiting.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── dispatch ──────────────────────────────────────────────────────────

    async def _handle_request(self, msg: dict[str, Any]) -> None:
        request_id = msg["id"]
        method = msg["method"]
        params: dict[str, Any] = msg.get("params") or {}
        try:
            result = await self.dispatch(method, params)
            await self._conn.respond(request_id, result)
        except JsonRpcError as exc:
            await self._conn.respond_error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            log.exception("acp request %s failed", method)
            await self._conn.respond_error(request_id, -32603, f"internal error: {exc}")

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Route one JSON-RPC request method to its handler and return the result.

        The public request surface: :meth:`serve` calls this per inbound request,
        and it is the seam tests drive directly. Raises :class:`JsonRpcError` for
        unknown methods or invalid params.
        """
        if method == "initialize":
            return self._initialize(params)
        if method == "session/new":
            return self._session_new(params)
        if method == "session/prompt":
            return await self._session_prompt(params)
        raise JsonRpcError(METHOD_NOT_FOUND, f"method not found: {method}")

    async def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """Route one JSON-RPC notification (no response). The public notify surface."""
        if method == "session/cancel":
            self._session_cancel(params)
        else:
            log.debug("ignoring notification: %s", method)

    # ── methods ─────────────────────────────────────────────────────────────

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
            },
        }

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._sessions.create()
        return {"sessionId": session.id}

    def _session_cancel(self, params: dict[str, Any]) -> None:
        # Signal the turn loop; it owns the single cancel of the driver task so
        # the orchestrator's CancelledError cleanup runs without a double-cancel.
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            return
        cancel = self._cancels.get(session_id)
        if cancel is not None:
            cancel.set()

    async def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            raise JsonRpcError(INVALID_PARAMS, "sessionId is required")
        session = self._sessions.get(session_id)
        if session is None:
            raise JsonRpcError(INVALID_PARAMS, f"unknown session: {session_id}")
        text = _prompt_text(params.get("prompt"))
        if not text:
            raise JsonRpcError(INVALID_PARAMS, "prompt must contain at least one text block")
        stop_reason = await self._run_turn(session, text)
        return {"stopReason": stop_reason}

    # ── turn execution + event bridge ────────────────────────────────────────

    async def _run_turn(self, session: Session, text: str) -> str:
        store = self._rt.store
        task_id = new_task_id()
        # Dovetail: a follow-up prompt carries the prior task's result forward.
        state = ExecutionState(task_id=task_id, user_request=session.compose_request(text))
        await store.save_task(state)
        session.tasks.append(task_id)
        session.active_task = task_id

        tracker = PlanTracker()
        cancel = asyncio.Event()
        self._cancels[session.id] = cancel
        stop_reason = "end_turn"
        # The currently-driving orchestrator coroutine. It is replaced when the
        # plan gate resolves (resume on approve, revise on reject-with-feedback);
        # the turn is driven entirely by the events these emit, never by the
        # task's completion, so a pause at the gate doesn't look like an ending.
        current = asyncio.create_task(self._rt.orchestrator.run(task_id))
        async with self._rt.event_bus.subscribe() as sub:
            try:
                while True:
                    event = await self._next_event(sub, cancel)
                    if event is None:
                        stop_reason = "cancelled"
                        break
                    if event.task_id != task_id:
                        continue
                    if isinstance(event, AwaitingConfirmation):
                        outcome = await self._handle_gate(session, tracker, task_id)
                        if isinstance(outcome, str):
                            stop_reason = outcome
                            break
                        current = outcome
                        continue
                    if isinstance(event, ToolApprovalRequested):
                        await self._handle_tool_approval(session, event)
                        continue
                    terminal = await self._translate(session, tracker, event)
                    if terminal is not None:
                        stop_reason = terminal
                        break
            finally:
                session.active_task = None
                self._cancels.pop(session.id, None)
                if not current.done():
                    current.cancel()
                # Drain the driver: its own failures already surfaced as
                # TaskFailed, and cancellation is the expected session/cancel path.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await current

        final = await store.load_task(task_id)
        if final is not None and final.final_output:
            session.carried_context = final.final_output
        return stop_reason

    async def _next_event(self, sub: Any, cancel: asyncio.Event) -> Event | None:
        """Await the next task event, or ``None`` if cancellation fires first."""
        event_task: asyncio.Task[Event] = asyncio.ensure_future(sub.__anext__())
        cancel_task = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({event_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
        if not event_task.done():
            event_task.cancel()
            return None
        try:
            return event_task.result()
        except StopAsyncIteration:
            return None

    async def _handle_gate(
        self, session: Session, tracker: PlanTracker, task_id: TaskId
    ) -> asyncio.Task[None] | str:
        """Ask the client to approve the plan; return the next driver or a stop reason.

        Approve → resume the task. Reject with feedback → guided replan (loops
        back to the gate). Reject without feedback → ``"refusal"``; cancel →
        ``"cancelled"``. The free-text ``feedback`` is our extension to the ACP
        permission response; a generic client that omits it gets a plain reject.
        """
        outcome = await self._conn.request(
            "session/request_permission",
            {
                "sessionId": session.id,
                "plan": {"entries": tracker.entries()},
                "options": [
                    {
                        "optionId": "approve",
                        "name": "Approve and run this plan",
                        "kind": "allow_once",
                    },
                    {"optionId": "reject", "name": "Reject the plan", "kind": "reject_once"},
                ],
            },
        )
        choice, feedback = _parse_permission(outcome)
        if choice == "approve":
            return asyncio.create_task(self._rt.orchestrator.resume(task_id))
        if choice == "reject" and feedback:
            return asyncio.create_task(self._rt.orchestrator.revise(task_id, feedback))
        if choice == "reject":
            return "refusal"
        return "cancelled"

    async def _handle_tool_approval(self, session: Session, event: ToolApprovalRequested) -> None:
        broker = self._rt.tool_approval
        if broker is None:
            return
        outcome = await self._conn.request(
            "session/request_permission",
            {
                "sessionId": session.id,
                "kind": "tool_call",
                "toolCall": {
                    "approvalId": event.approval_id,
                    "nodeId": event.node_id,
                    "toolName": event.tool_name,
                    "risk": event.risk,
                    "args": event.args,
                    "description": event.description,
                },
                "options": [
                    {
                        "optionId": "allow_once",
                        "name": f"Allow {event.tool_name} once",
                        "kind": "allow_once",
                    },
                    {
                        "optionId": "always_allow",
                        "name": f"Always allow {event.tool_name}",
                        "kind": "allow_always",
                    },
                    {"optionId": "deny", "name": "Deny this tool call", "kind": "reject_once"},
                ],
            },
        )
        choice, _ = _parse_permission(outcome)
        if choice == "always_allow":
            broker.remember_allow(event.tool_name)
            broker.resolve(event.approval_id, True)
        elif choice == "allow_once":
            broker.resolve(event.approval_id, True)
        else:
            broker.resolve(event.approval_id, False)

    async def _translate(self, session: Session, tracker: PlanTracker, event: Event) -> str | None:
        """Emit session/update(s) for one event; return a stop reason if terminal."""
        if isinstance(event, NodeOutputChunk):
            await self._message(
                session,
                event.text,
                node_id=event.node_id,
                node_type=event.node_type.value,
                is_final=False,
            )
        elif isinstance(event, PlanReady) and event.plan is not None:
            tracker.load(event.plan)
            await self._update(session, {"sessionUpdate": "plan", "entries": tracker.entries()})
            await self._update(session, _plan_graph(event.plan))
        elif isinstance(event, NodeStarted):
            await self._translate_node_started(session, tracker, event)
        elif isinstance(event, NodeFinished):
            tracker.finish(event.node_id, event.status)
            await self._update(session, {"sessionUpdate": "plan", "entries": tracker.entries()})
            await self._emit_node_output(session, tracker, event)
            await self._emit_node_meta(session, tracker, event)
        elif isinstance(event, BranchDecided):
            if event.branch_taken:
                await self._message(session, f"→ branch: {event.branch_taken}")
        else:
            return await self._translate_terminal(session, event)
        return None

    async def _translate_node_started(
        self, session: Session, tracker: PlanTracker, event: NodeStarted
    ) -> None:
        tracker.start(event.node_id)
        await self._update(session, {"sessionUpdate": "plan", "entries": tracker.entries()})
        if event.node_type.value == "tool":
            await self._update(
                session,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": _tool_call_id(event.node_id),
                    "title": tracker.description(event.node_id) or "tool",
                    "kind": _TOOL_KINDS.get(tracker.tool_name(event.node_id) or "", "other"),
                    "status": "in_progress",
                    "rawInput": tracker.tool_input(event.node_id),
                },
            )

    async def _translate_terminal(self, session: Session, event: Event) -> str | None:
        """Handle terminal and no-op events; return a stop reason or None."""
        if isinstance(event, TaskCompleted):
            if event.final_output:
                await self._message(session, event.final_output, node_type="result", is_final=True)
            return "end_turn"
        if isinstance(event, TaskFailed):
            await self._message(session, f"Task failed: {event.error}")
            return "end_turn"
        if isinstance(event, PlanFailed):
            msg = f"Planning failed: {event.error}"
            if event.filepath:
                msg += f"\nFailed plan written to: {event.filepath}"
            await self._message(session, msg)
            return "end_turn"
        if isinstance(event, PlanRejected):
            await self._message(session, f"Task rejected: {event.reason}")
            return "end_turn"
        if isinstance(event, TaskCancelled):
            return "cancelled"
        return None

    async def _emit_node_output(
        self, session: Session, tracker: PlanTracker, event: NodeFinished
    ) -> None:
        node_type = tracker.node_type(event.node_id)
        state = await self._rt.store.load_task(event.task_id)
        result = state.results.get(event.node_id) if state is not None else None
        if result is None:
            return
        if node_type == "tool":
            await self._update(
                session,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": _tool_call_id(event.node_id),
                    "status": "failed" if event.status is NodeStatus.FAILED else "completed",
                    "rawOutput": {"output": _as_text(result.output)},
                },
            )
        elif node_type in _MESSAGE_NODE_TYPES:
            pass  # output was already streamed live via NodeOutputChunk events

    async def _emit_node_meta(
        self, session: Session, tracker: PlanTracker, event: NodeFinished
    ) -> None:
        """Stream per-node tier/confidence as a dagagent ACP extension.

        ``node_meta`` is not a standard ACP update kind; a generic client ignores
        it, while our TUI renders a dim tier/confidence annotation. Only emitted
        for nodes that actually ran (completed or failed), not skipped ones.
        """
        if event.status not in (NodeStatus.COMPLETED, NodeStatus.FAILED):
            return
        await self._update(
            session,
            {
                "sessionUpdate": "node_meta",
                "nodeId": event.node_id,
                "nodeType": tracker.node_type(event.node_id) or "node",
                "tier": event.tier_used,
                "confidence": event.confidence,
                "flagged": event.flagged,
            },
        )

    # ── update helpers ────────────────────────────────────────────────────

    async def _update(self, session: Session, update: dict[str, Any]) -> None:
        await self._conn.notify("session/update", {"sessionId": session.id, "update": update})

    async def _message(
        self,
        session: Session,
        text: str,
        *,
        node_id: int | None = None,
        node_type: str | None = None,
        is_final: bool = False,
    ) -> None:
        update: dict[str, Any] = {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
            "isFinal": is_final,
        }
        if node_id is not None:
            update["nodeId"] = node_id
        if node_type is not None:
            update["nodeType"] = node_type
        await self._update(
            session,
            update,
        )


def _plan_graph(plan: dict[str, Any]) -> dict[str, Any]:
    """A dagagent extension carrying the plan's graph topology.

    ACP's ``plan`` update is a flat entry list with no edges; the TUI's DAG view
    needs the dependency structure, so we send node types plus explicit data and
    control edges once per plan. ``deps`` stays on each node for older clients.
    Generic ACP clients ignore the unknown update kind.
    """
    nodes: list[dict[str, Any]] = plan.get("nodes") or []
    order: list[int] = plan.get("sorted_node_ids") or [n["id"] for n in nodes]
    by_id: dict[int, dict[str, Any]] = {n["id"]: n for n in nodes}
    out: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for node_id in order:
        node = by_id.get(node_id)
        if node is None:
            continue
        deps = [d for d in (node.get("context_needed") or []) if d in by_id]
        for dep in deps:
            edges.append({"from": dep, "to": node_id, "kind": "data"})
        branches = node.get("branches")
        if isinstance(branches, dict):
            branch_map = cast("dict[object, Any]", branches)
            for label, targets in branch_map.items():
                if not isinstance(targets, list):
                    continue
                for target in targets:
                    if isinstance(target, int) and not isinstance(target, bool) and target in by_id:
                        edges.append(
                            {
                                "from": node_id,
                                "to": target,
                                "kind": "control",
                                "label": str(label),
                            }
                        )
        out.append(
            {
                "id": node_id,
                "nodeType": node.get("type", "node"),
                "description": node.get("description", ""),
                "deps": deps,
            }
        )
    return {"sessionUpdate": "plan_graph", "nodes": out, "edges": edges}


def _prompt_text(prompt: Any) -> str:
    """Concatenate the text of ACP content blocks into a single request string."""
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, list):
        return ""
    parts: list[str] = []
    for raw in cast("list[Any]", prompt):
        if isinstance(raw, dict):
            block = cast("dict[str, Any]", raw)
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _parse_permission(outcome: Any) -> tuple[str, str | None]:
    """Read an ACP request_permission response into (choice, feedback).

    Accepts the nested ``{"outcome": {"outcome": "selected", "optionId": ...}}``
    shape and a flattened ``{"outcome": "selected", "optionId": ...}`` variant,
    and reads our optional free-text ``feedback`` from either level. Anything
    unrecognised is treated as a cancel.
    """
    if not isinstance(outcome, dict):
        return "cancelled", None
    top = cast("dict[str, Any]", outcome)
    inner = top.get("outcome")
    body: dict[str, Any] = inner if isinstance(inner, dict) else top
    kind = body.get("outcome") if isinstance(inner, dict) else inner
    if kind != "selected":
        return "cancelled", None
    option = body.get("optionId")
    raw_feedback = body.get("feedback") or top.get("feedback")
    feedback = raw_feedback if isinstance(raw_feedback, str) and raw_feedback.strip() else None
    return (str(option), feedback)


def _tool_call_id(node_id: int) -> str:
    return f"node-{node_id}"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
