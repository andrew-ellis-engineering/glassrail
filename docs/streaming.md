# Streaming events

A task runs asynchronously: `POST /task` returns a `task_id` immediately and
the work proceeds in the background. To follow it live, subscribe to the
task's event stream over **Server-Sent Events** or a **WebSocket** — both at
`/task/{id}/events`, both carrying the same typed events.

The events are the typed Pydantic models the executor and orchestrator publish
to the in-process `EventBus` (`PlanningStarted`, `PlanReady`, `NodeStarted`,
`NodeFinished`, `BranchDecided`, `TaskCompleted`, `TaskFailed`, ...). Each is
serialised as JSON with a `type` discriminator and a `task_id`. The stream
ends after the first terminal event (`task_completed`, `task_failed`, or
`awaiting_confirmation`).

If you connect *after* the task already finished, you don't miss out: the
server synthesises a single snapshot event for the terminal state and closes.

## Server-Sent Events

```bash
curl -N http://localhost:8000/task/$TASK_ID/events
```

```
data: {"type": "planning_started", "task_id": "01K..."}
data: {"type": "node_finished", "task_id": "01K...", "node_id": 1, "status": "completed", ...}
data: {"type": "task_completed", "task_id": "01K...", "final_output": "..."}
```

Each event is one `data:` frame. The response closes after the terminal event.
Unknown task → `404`; no event bus configured → `503`.

## WebSocket

```python
from websockets.sync.client import connect

with connect(f"ws://localhost:8000/task/{task_id}/events") as ws:
    while True:
        print(ws.recv())   # one JSON event per message; raises on close
```

Each event arrives as one text message (the same JSON as SSE). The server
closes the socket once a terminal event has been sent. Connections are
rejected *before* the handshake completes when the request is invalid, so the
client sees a close code rather than a silently dropped stream:

| Condition | Close code |
|-----------|-----------|
| Unknown task | `1008` (policy violation) |
| No event bus configured | `1011` (internal error) |

## One source, two transports

Both endpoints consume a single transport-agnostic generator
(`_event_source`) that owns the subscribe-then-snapshot-or-stream logic. SSE
wraps each event in a `data:` frame; the WebSocket sends it as a text message.
Adding another transport later means consuming that same generator — the
producers don't change.
