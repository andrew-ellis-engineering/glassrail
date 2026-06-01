"""Planner-facing digest of registered tool capabilities.

The raw JSON schemas remain authoritative for tool names and arguments. This
digest is a quick capability map so the planner can choose a recipe/tool family
without scanning every schema from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from dagagent.harness.registry import ToolSchema


@dataclass(frozen=True)
class _ToolInfo:
    name: str
    description: str
    required: tuple[str, ...]
    risk: str = "read"


@dataclass(frozen=True)
class _CapabilityGroup:
    id: str
    title: str
    keywords: tuple[str, ...]


_GROUPS: tuple[_CapabilityGroup, ...] = (
    _CapabilityGroup(
        id="web_information",
        title="Web / current information",
        keywords=("web", "search", "fetch", "url", "rss", "page"),
    ),
    _CapabilityGroup(
        id="filesystem",
        title="Filesystem / local files",
        keywords=("file", "path", "read", "write", "glob", "tree"),
    ),
    _CapabilityGroup(
        id="calendar_time",
        title="Calendar / time",
        keywords=("calendar", "event", "schedule", "date", "reminder", "time"),
    ),
    _CapabilityGroup(
        id="memory_knowledge",
        title="Memory / knowledge base",
        keywords=("memory", "note", "obsidian", "knowledge", "bookmark"),
    ),
    _CapabilityGroup(
        id="communication",
        title="Communication",
        keywords=("email", "message", "sms", "slack", "discord", "send"),
    ),
    _CapabilityGroup(
        id="coding_system",
        title="Coding / system operations",
        keywords=("git", "shell", "test", "lint", "code", "diff", "command"),
    ),
    _CapabilityGroup(
        id="data_documents",
        title="Data / documents",
        keywords=("csv", "json", "sql", "spreadsheet", "pdf", "document"),
    ),
)

_PREFIX_GROUPS: tuple[tuple[str, str], ...] = (
    ("web_", "web_information"),
    ("file_", "filesystem"),
    ("calendar_", "calendar_time"),
    ("memory_", "memory_knowledge"),
    ("note_", "memory_knowledge"),
    ("email_", "communication"),
    ("sms_", "communication"),
    ("git_", "coding_system"),
)


def render_tool_capability_digest(schemas: list[ToolSchema]) -> str:
    """Render a compact planner prompt section for the registered tools."""
    tools = [_tool_info(schema) for schema in schemas]
    grouped: dict[str, list[_ToolInfo]] = {group.id: [] for group in _GROUPS}
    other: list[_ToolInfo] = []

    for tool in tools:
        group = _classify(tool)
        if group is None:
            other.append(tool)
        else:
            grouped[group.id].append(tool)

    lines = [
        "Tool capability digest:",
        "- Use this as a quick capability map; the full JSON schemas below are "
        "authoritative for exact tool names and arguments.",
        "- If a needed capability is absent, emit a rejection instead of inventing a tool.",
        "- Risk levels: [read] no side effects; [network] reads external sources; "
        "[write] modifies local state; [execute] runs code — plan accordingly.",
    ]
    for group in _GROUPS:
        entries = grouped[group.id]
        if entries:
            lines.append(f"- {group.title}: {_format_tools(entries)}")
    if other:
        lines.append(f"- Other registered tools: {_format_tools(other)}")
    if not tools:
        lines.append("- No tools are registered.")
    return "\n".join(lines)


def _tool_info(schema: ToolSchema) -> _ToolInfo:
    raw_fn = schema.get("function")
    if not isinstance(raw_fn, dict):
        return _ToolInfo(name="<unknown>", description="", required=())

    fn = cast("dict[str, object]", raw_fn)
    name = str(fn.get("name", "<unknown>"))
    description = str(fn.get("description", ""))
    risk = str(schema.get("x_risk", "read"))
    raw_parameters = fn.get("parameters")
    required: tuple[str, ...] = ()
    if isinstance(raw_parameters, dict):
        parameters = cast("dict[str, object]", raw_parameters)
        raw_required = parameters.get("required")
        if isinstance(raw_required, list):
            required = tuple(str(item) for item in cast("list[object]", raw_required))
    return _ToolInfo(name=name, description=description, required=required, risk=risk)


def _classify(tool: _ToolInfo) -> _CapabilityGroup | None:
    for prefix, group_id in _PREFIX_GROUPS:
        if tool.name.startswith(prefix):
            return _group_by_id(group_id)
    text = f"{tool.name} {tool.description}".lower()
    for group in _GROUPS:
        if any(keyword in text for keyword in group.keywords):
            return group
    return None


def _group_by_id(group_id: str) -> _CapabilityGroup:
    for group in _GROUPS:
        if group.id == group_id:
            return group
    raise ValueError(f"Unknown capability group: {group_id}")


def _format_tools(tools: list[_ToolInfo]) -> str:
    return "; ".join(_format_tool(tool) for tool in sorted(tools, key=lambda item: item.name))


def _format_tool(tool: _ToolInfo) -> str:
    required = f" required={list(tool.required)}" if tool.required else ""
    description = f" — {tool.description}" if tool.description else ""
    return f"{tool.name}[{tool.risk}]{required}{description}"
