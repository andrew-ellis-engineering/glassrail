"""Tests for planner tool capability digests."""

from __future__ import annotations

from dagagent.harness import ToolHarness, register_builtins
from dagagent.planner.tool_digest import render_tool_capability_digest


def test_tool_digest_groups_builtin_capabilities() -> None:
    harness = ToolHarness()
    register_builtins(harness)

    digest = render_tool_capability_digest(harness.all_schemas())

    assert "Tool capability digest:" in digest
    assert "Filesystem / local files: file_read" in digest
    assert "Calendar / time: calendar_get" in digest
    assert "Memory / knowledge base: memory_search" in digest
    assert "full JSON schemas below are authoritative" in digest


def test_tool_digest_groups_web_tools_when_registered() -> None:
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web and return result snippets.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch a web page by URL.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
    ]

    digest = render_tool_capability_digest(schemas)

    assert "Web / current information:" in digest
    assert "web_search required=['query']" in digest
    assert "web_fetch required=['url']" in digest
    assert "inventing a tool" in digest


def test_tool_digest_handles_no_tools() -> None:
    digest = render_tool_capability_digest([])

    assert "No tools are registered." in digest
