"""Thin async client for a running gateway — submit a task, stream its events.

Decoupled from the server's Pydantic models: it yields the raw decoded event
dicts off the SSE stream, which the :class:`~dagagent.gateways.tui.view.TaskView`
reads defensively. The ``client`` parameter is injectable so tests can drive it
with an ``httpx.MockTransport`` instead of a live server.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"


async def stream_task_events(
    request: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Submit ``request`` and yield each task event as it arrives.

    POSTs ``/task``, then consumes the SSE stream at ``/task/{id}/events``,
    yielding one decoded event dict per ``data:`` frame until the stream ends.
    """
    own_client = client is None
    client = client or httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(None))
    try:
        resp = await client.post("/task", json={"request": request})
        resp.raise_for_status()
        task_id = str(resp.json()["task_id"])

        async with client.stream("GET", f"/task/{task_id}/events") as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                event: Any = json.loads(payload)
                if isinstance(event, dict):
                    yield event
    finally:
        if own_client:
            await client.aclose()
