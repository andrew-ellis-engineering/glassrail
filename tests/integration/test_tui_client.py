"""Integration tests for the TUI client and runner.

A mock transport stands in for a running gateway: POST /task returns a task id,
GET /task/{id}/events returns a canned SSE body. The client and the full
``run_tui`` loop drive against it — no real server, no real terminal.
"""

from __future__ import annotations

import io
import json
from typing import Any

import httpx
from rich.console import Console

from dagagent.gateways.tui import run_tui, stream_task_events

_TASK_ID = "01TESTTASKID"
_EVENTS: list[dict[str, Any]] = [
    {"type": "planning_started", "task_id": _TASK_ID},
    {"type": "plan_ready", "task_id": _TASK_ID, "node_count": 2},
    {"type": "node_started", "task_id": _TASK_ID, "node_id": 1, "node_type": "tool", "tier": 0},
    {
        "type": "node_finished",
        "task_id": _TASK_ID,
        "node_id": 1,
        "status": "completed",
        "confidence": 1.0,
        "flagged": False,
    },
    {"type": "task_completed", "task_id": _TASK_ID, "final_output": "done."},
]


def _sse_body(events: list[dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/task":
        return httpx.Response(202, json={"task_id": _TASK_ID, "status": "planning"})
    if request.method == "GET" and request.url.path == f"/task/{_TASK_ID}/events":
        return httpx.Response(
            200, text=_sse_body(_EVENTS), headers={"content-type": "text/event-stream"}
        )
    return httpx.Response(404)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(_handler))


async def test_stream_task_events_yields_decoded_events() -> None:
    async with _client() as client:
        got = [event async for event in stream_task_events("hi", client=client)]
    assert [e["type"] for e in got] == [e["type"] for e in _EVENTS]
    assert got[-1]["final_output"] == "done."


async def test_run_tui_renders_to_completion() -> None:
    buf = io.StringIO()
    console = Console(file=buf, width=100)
    async with _client() as client:
        view = await run_tui("hi", console=console, client=client)

    assert view.status == "completed"
    assert view.final_output == "done."
    assert view.nodes[1].node_type == "tool"
