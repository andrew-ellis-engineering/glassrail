"""ToolHarness — the registry every executor consults.

First-party tools register at import time via ``@harness.tool(...)``.
Third-party tools advertise themselves through the ``dagagent.tools``
entry-point group; :meth:`ToolHarness.load_entry_points` discovers and
registers them at startup.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from dagagent.core import ToolExecutionError, ToolRegistrationError

log = logging.getLogger(__name__)

ToolFunc = Callable[..., Awaitable[Any] | Any]
"""A registered tool — sync or async, kwargs-only."""

ToolSchema = dict[str, Any]
"""OpenAI-style tool schema: ``{"type": "function", "function": {...}}``."""

ToolRisk = Literal["read", "network", "write", "execute"]
"""Declared side-effect risk for a tool.

- ``read``    — no side effects; reads local data only (default).
- ``network`` — reads from external sources; may leak information or incur cost.
- ``write``   — modifies local state (files, database, etc.).
- ``execute`` — runs arbitrary code or shell commands; highest risk.

``read`` and ``network`` tools run without user confirmation. ``write`` and
``execute`` tools are intended to require explicit user approval via the
HITL gate — callers should check :meth:`ToolHarness.risk_for` before
executing any tool with risk ``write`` or ``execute``.
"""


class ToolHarness:
    """Registry of callable tools with JSON-schema metadata."""

    def __init__(self) -> None:
        self._funcs: dict[str, ToolFunc] = {}
        self._schemas: dict[str, ToolSchema] = {}
        self._risk: dict[str, ToolRisk] = {}

    # ── Registration ─────────────────────────────────────────────────────

    def tool(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        risk: ToolRisk = "read",
    ) -> Callable[[ToolFunc], ToolFunc]:
        """Register a tool. Use as a decorator factory.

        ``risk`` declares the tool's side-effect level and governs whether
        execution requires user approval. Default is ``"read"`` (safe).

        Example::

            @harness.tool(
                name="calendar_get",
                description="Fetch calendar events for a date",
                parameters={"type": "object", "properties": {...}, "required": [...]},
                risk="read",
            )
            async def calendar_get(date: str) -> dict:
                ...
        """

        def decorator(func: ToolFunc) -> ToolFunc:
            self._register(
                name=name, description=description, parameters=parameters, func=func, risk=risk
            )
            return func

        return decorator

    def _register(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: ToolFunc,
        risk: ToolRisk = "read",
    ) -> None:
        if name in self._funcs:
            raise ToolRegistrationError(f"Tool '{name}' already registered")
        self._funcs[name] = func
        self._schemas[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
            # Extension field: not part of the OpenAI function-calling spec but
            # ignored by compliant parsers. Used by the planner digest and the
            # executor's HITL gate.
            "x_risk": risk,
        }
        self._risk[name] = risk
        log.info("Registered tool: %s (risk=%s)", name, risk)

    def load_entry_points(self, group: str = "dagagent.tools") -> int:
        """Discover and register tools exposed via the ``dagagent.tools`` group.

        Returns the number of tools loaded. Safe to call more than once;
        already-registered names are skipped with a warning rather than raised.
        """
        loaded = 0
        for ep in importlib.metadata.entry_points(group=group):
            try:
                obj = ep.load()
            except Exception:
                log.exception("Failed to load entry point %s", ep.name)
                continue
            if not callable(obj):
                log.warning("Entry point %s did not resolve to a callable", ep.name)
                continue
            if ep.name in self._funcs:
                log.warning("Entry point %s already registered; skipping", ep.name)
                continue
            # Plugins are expected to call harness.tool themselves and expose
            # the result. If they expose a plain function, we attach a minimal
            # schema and treat the docstring as the description.
            if ep.name not in self._funcs:
                self._register(
                    name=ep.name,
                    description=(obj.__doc__ or "").strip() or ep.name,
                    parameters={"type": "object", "properties": {}},
                    func=obj,
                )
                loaded += 1
        return loaded

    # ── Lookup ───────────────────────────────────────────────────────────

    def schema_for(self, name: str) -> ToolSchema | None:
        return self._schemas.get(name)

    def risk_for(self, name: str) -> ToolRisk:
        """Return the declared risk level for ``name``, defaulting to ``"read"``."""
        return self._risk.get(name, "read")

    def all_schemas(self) -> list[ToolSchema]:
        return list(self._schemas.values())

    def all_names(self) -> set[str]:
        return set(self._funcs)

    def unknown_names(self, names: list[str | None]) -> list[str]:
        """Return any names from ``names`` that are not registered.

        ``None`` entries are ignored so callers can pass ``[step.tool for step ...]``
        without filtering first.
        """
        return [n for n in names if n is not None and n not in self._funcs]

    # ── Execution ────────────────────────────────────────────────────────

    async def execute(self, name: str, args: dict[str, Any]) -> Any:
        """Invoke a registered tool by name with keyword arguments."""
        func = self._funcs.get(name)
        if func is None:
            raise ToolExecutionError(f"Unknown tool: '{name}'")
        try:
            if inspect.iscoroutinefunction(func):
                return await func(**args)
            return func(**args)
        except Exception as exc:
            raise ToolExecutionError(f"Tool '{name}' raised: {exc}") from exc
