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

from dagagent.core import ExecutionState, NodeStatus, new_task_id
from dagagent.events import (
    BranchDecided,
    Event,
    NodeFinished,
    NodeStarted,
    PlanReady,
    TaskCompleted,
    TaskFailed,
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
_MESSAGE_NODE_TYPES = frozenset({"think", "summary", "synthesis"})


class AcpServer:
    """Speaks ACP over one stdio connection, backed by one agent runtime."""

    def __init__(self, runtime: Runtime, conn: Connection) -> None:
        self._rt = runtime
        self._conn = conn
        self._sessions = SessionRegistry()
        self._runs: dict[str, asyncio.Task[None]] = {}

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
                await self._handle_notification(method, params)
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

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
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
        session_id = params.get("sessionId")
        run = self._runs.get(session_id) if isinstance(session_id, str) else None
        if run is not None and not run.done():
            run.cancel()

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
        state = ExecutionState(task_id=task_id, user_request=text)
        await store.save_task(state)
        session.tasks.append(task_id)
        session.active_task = task_id

        tracker = PlanTracker()
        stop_reason = "end_turn"
        async with self._rt.event_bus.subscribe() as sub:
            run = asyncio.create_task(self._rt.orchestrator.run(task_id))
            self._runs[session.id] = run
            try:
                while True:
                    next_event: asyncio.Task[Event] = asyncio.ensure_future(sub.__anext__())
                    done, _ = await asyncio.wait(
                        {next_event, run}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if next_event in done:
                        try:
                            event = next_event.result()
                        except StopAsyncIteration:
                            break
                        if event.task_id != task_id:
                            continue
                        terminal = await self._translate(session, tracker, event)
                        if terminal is not None:
                            stop_reason = terminal
                            break
                    else:
                        # The run ended without a terminal event reaching us —
                        # cancellation or an unexpected stop. Drop the pending read.
                        next_event.cancel()
                        stop_reason = "cancelled" if run.cancelled() else "end_turn"
                        break
            finally:
                session.active_task = None
                self._runs.pop(session.id, None)
                if not run.done():
                    run.cancel()
                # Drain the run; its own failures already surfaced as TaskFailed,
                # and cancellation is expected on a client-driven session/cancel.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run

        final = await store.load_task(task_id)
        if final is not None and final.final_output:
            session.carried_context = final.final_output
        return stop_reason

    async def _translate(self, session: Session, tracker: PlanTracker, event: Event) -> str | None:
        """Emit session/update(s) for one event; return a stop reason if terminal."""
        if isinstance(event, PlanReady) and event.plan is not None:
            tracker.load(event.plan)
            await self._update(session, {"sessionUpdate": "plan", "entries": tracker.entries()})
        elif isinstance(event, NodeStarted):
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
                    },
                )
        elif isinstance(event, NodeFinished):
            tracker.finish(event.node_id, event.status)
            await self._update(session, {"sessionUpdate": "plan", "entries": tracker.entries()})
            await self._emit_node_output(session, tracker, event)
        elif isinstance(event, BranchDecided):
            if event.branch_taken:
                await self._message(session, f"→ branch: {event.branch_taken}")
        elif isinstance(event, TaskCompleted):
            if event.final_output:
                await self._message(session, event.final_output)
            return "end_turn"
        elif isinstance(event, TaskFailed):
            await self._message(session, f"Task failed: {event.error}")
            return "end_turn"
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
        elif node_type in _MESSAGE_NODE_TYPES and result.output is not None:
            text = _as_text(result.output)
            if text.strip():
                await self._message(session, text)

    # ── update helpers ────────────────────────────────────────────────────

    async def _update(self, session: Session, update: dict[str, Any]) -> None:
        await self._conn.notify("session/update", {"sessionId": session.id, "update": update})

    async def _message(self, session: Session, text: str) -> None:
        await self._update(
            session,
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        )


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


def _tool_call_id(node_id: int) -> str:
    return f"node-{node_id}"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
