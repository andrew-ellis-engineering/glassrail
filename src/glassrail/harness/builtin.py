"""Built-in tools registered by default.

``calendar_get`` and ``memory_search`` are placeholders that return empty
results — they exist so the planner has a non-empty toolset to plan around.
Replace them with real implementations (or remove from the registry) for
production use. Web search is no longer a stub here: it lives in the opt-in
web integration (``glassrail.harness.integrations.web``).

``file_read`` is a real implementation, intentionally simple.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from glassrail.harness.registry import ToolHarness


async def calendar_get(date: str) -> dict[str, Any]:
    """Fetch calendar events for a given date (stub)."""
    return {"date": date, "events": [], "source": "stub"}


async def memory_search(query: str, limit: int = 5) -> dict[str, Any]:
    """Search the agent's long-term memory store (stub)."""
    return {"query": query, "limit": limit, "results": [], "source": "stub"}


async def file_read(path: str) -> dict[str, Any]:
    """Read a UTF-8 text file from the local filesystem."""
    try:
        content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    return {"path": path, "content": content}


async def eval_noop() -> dict[str, Any]:
    """Eval-only no-op tool — always returns an empty dict.

    Used by harness-mechanics tests to exercise the executor's empty-result
    (NodeStatus.EMPTY) code path without requiring real infrastructure.
    Never register this in production; it is a test fixture only.
    """
    return {}


def register_builtins(harness: ToolHarness) -> None:
    """Attach every built-in tool to ``harness``."""
    harness.tool(
        name="calendar_get",
        description="Fetch calendar events for a given date (stub).",
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["date"],
        },
        risk="read",
    )(calendar_get)

    harness.tool(
        name="memory_search",
        description="Search the agent's long-term memory store (stub).",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        risk="read",
    )(memory_search)

    harness.tool(
        name="file_read",
        description="Read a UTF-8 text file from the local filesystem.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
        risk="read",
    )(file_read)


def register_eval_tools(harness: ToolHarness) -> None:
    """Attach eval-only tools used by fixed-plan harness tests."""

    harness.tool(
        name="eval_noop",
        description="Eval-only no-op — always returns an empty result (test fixture only).",
        parameters={"type": "object", "properties": {}, "required": []},
        risk="read",
    )(eval_noop)
