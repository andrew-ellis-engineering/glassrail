"""The live runner — glue between the event client and the view.

Drives a ``rich.Live`` display: submit the task, then update the view as each
event arrives. ``console`` and ``client`` are injectable so a test can run the
whole loop against a mock transport with a non-terminal console.
"""

from __future__ import annotations

import httpx
from rich.console import Console
from rich.live import Live

from glassrail.gateways.tui.client import DEFAULT_BASE_URL, stream_task_events
from glassrail.gateways.tui.view import TaskView


async def run_tui(
    request: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    show_dag: bool = True,
    console: Console | None = None,
    client: httpx.AsyncClient | None = None,
) -> TaskView:
    """Submit ``request`` and render its live event stream. Returns the final view."""
    view = TaskView(request=request, show_dag=show_dag)
    console = console or Console()
    with Live(view.render(), console=console, refresh_per_second=8) as live:
        async for event in stream_task_events(request, base_url=base_url, client=client):
            view.ingest(event)
            live.update(view.render())
    return view
