"""Tests for planner tool capability digests."""

from __future__ import annotations

from glassrail.harness import ToolHarness, register_builtins
from glassrail.planner.tool_digest import render_tool_capability_digest


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
    # Risk tag is now embedded: name[risk] required=[...]
    assert "web_search[read] required=['query']" in digest
    assert "web_fetch[read] required=['url']" in digest
    assert "inventing a tool" in digest
    assert "Risk levels:" in digest


def test_tool_risk_is_stored_and_retrievable() -> None:
    harness = ToolHarness()
    register_builtins(harness)

    assert harness.risk_for("file_read") == "read"
    assert harness.risk_for("calendar_get") == "read"
    # Unknown tool defaults to the safe "read" level.
    assert harness.risk_for("nonexistent_tool") == "read"


def test_tool_digest_includes_risk_tag_in_tool_line() -> None:
    harness = ToolHarness()
    register_builtins(harness)
    digest = render_tool_capability_digest(harness.all_schemas())
    assert "file_read[read]" in digest


def test_tool_digest_handles_no_tools() -> None:
    digest = render_tool_capability_digest([])

    assert "No tools are registered." in digest
