"""Tests for the tool harness registry."""

from __future__ import annotations

from typing import Any

import pytest

from dagagent.core import ToolExecutionError, ToolRegistrationError
from dagagent.harness import ToolHarness, register_builtins


@pytest.fixture
def harness() -> ToolHarness:
    return ToolHarness()


async def test_register_and_execute_async(harness: ToolHarness) -> None:
    @harness.tool(
        name="echo",
        description="Return the input",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
    )
    async def _echo(x: str) -> dict[str, str]:
        return {"x": x}

    result = await harness.execute("echo", {"x": "hi"})
    assert result == {"x": "hi"}


async def test_register_and_execute_sync(harness: ToolHarness) -> None:
    @harness.tool(
        name="double",
        description="Double an integer",
        parameters={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    def _double(n: int) -> int:
        return n * 2

    assert await harness.execute("double", {"n": 4}) == 8


async def test_duplicate_registration_raises(harness: ToolHarness) -> None:
    @harness.tool(name="t", description="d", parameters={"type": "object"})
    async def _first() -> None: ...

    with pytest.raises(ToolRegistrationError):

        @harness.tool(name="t", description="d", parameters={"type": "object"})
        async def _second() -> None: ...


async def test_unknown_tool_raises(harness: ToolHarness) -> None:
    with pytest.raises(ToolExecutionError):
        await harness.execute("nope", {})


async def test_tool_exception_wrapped(harness: ToolHarness) -> None:
    @harness.tool(name="boom", description="d", parameters={"type": "object"})
    async def _boom() -> None:
        raise RuntimeError("kapow")

    with pytest.raises(ToolExecutionError, match="kapow"):
        await harness.execute("boom", {})


def test_schemas_and_names(harness: ToolHarness) -> None:
    @harness.tool(name="a", description="da", parameters={"type": "object"})
    async def _a() -> None: ...

    @harness.tool(name="b", description="db", parameters={"type": "object"})
    async def _b() -> None: ...

    assert harness.all_names() == {"a", "b"}
    schemas = harness.all_schemas()
    assert {s["function"]["name"] for s in schemas} == {"a", "b"}
    assert harness.schema_for("a") is not None
    assert harness.schema_for("missing") is None


def test_unknown_names_filters_none_and_known(harness: ToolHarness) -> None:
    @harness.tool(name="known", description="d", parameters={"type": "object"})
    async def _known() -> None: ...

    assert harness.unknown_names(["known", "missing", None]) == ["missing"]


async def test_register_builtins(harness: ToolHarness) -> None:
    register_builtins(harness)
    assert harness.all_names() == {"calendar_get", "memory_search", "web_search", "file_read"}
    result: dict[str, Any] = await harness.execute("calendar_get", {"date": "2026-05-27"})
    assert result["date"] == "2026-05-27"
    assert result["events"] == []


async def test_file_read_real(harness: ToolHarness, tmp_path: Any) -> None:
    register_builtins(harness)
    p = tmp_path / "x.txt"
    p.write_text("hello there")
    out = await harness.execute("file_read", {"path": str(p)})
    assert out == {"path": str(p), "content": "hello there"}


async def test_file_read_missing_returns_error(harness: ToolHarness, tmp_path: Any) -> None:
    register_builtins(harness)
    missing = tmp_path / "nope.txt"
    out = await harness.execute("file_read", {"path": str(missing)})
    assert "error" in out
