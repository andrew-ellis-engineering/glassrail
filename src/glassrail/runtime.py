"""Composition root — assemble the agent's collaborators from :class:`Settings`.

One place wires the harness, router, planner, validator, executor, store, and
orchestrator together. The REST gateway and the CLI both build their runtime
here, so the wiring never drifts between entry points.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from glassrail.config import Settings, get_settings
from glassrail.events import EventBus
from glassrail.executor import Executor, Orchestrator
from glassrail.executor.tool_approval import ToolApprovalBroker
from glassrail.harness import ToolHarness, register_builtins
from glassrail.harness.integrations import register_integrations
from glassrail.planner import Planner
from glassrail.providers import TierRouter, router_from_settings
from glassrail.state import InMemoryStateStore, StateStore
from glassrail.telemetry import configure_tracing
from glassrail.validator import PlanValidator

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    """The assembled collaborators for one running agent."""

    orchestrator: Orchestrator
    store: StateStore
    harness: ToolHarness
    event_bus: EventBus
    settings: Settings
    tool_approval: ToolApprovalBroker | None = None
    router: TierRouter | None = None

    async def aclose(self) -> None:
        """Release runtime-owned network resources."""
        if self.router is not None:
            await self.router.aclose()


def build_runtime(
    settings: Settings | None = None,
    *,
    store: StateStore | None = None,
    interactive_tool_approval: bool = False,
) -> Runtime:
    """Wire a complete agent runtime from settings (defaults to in-memory state)."""
    settings = settings or get_settings()
    configure_tracing(settings)

    bus = EventBus()
    harness = ToolHarness()
    register_builtins(harness, fs_roots=settings.tools.fs_roots)
    register_integrations(harness, settings)
    if settings.load_tool_plugins:
        loaded = harness.load_entry_points()
        log.info("Loaded %d tool plugin(s) from the glassrail.tools entry-point group", loaded)
    router = router_from_settings(settings)
    tool_approval = ToolApprovalBroker(bus) if interactive_tool_approval else None
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator, settings=settings)
    executor = Executor(
        router=router,
        harness=harness,
        settings=settings,
        event_bus=bus,
        tool_approval=tool_approval,
    )
    store = store or InMemoryStateStore()
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
        event_bus=bus,
    )
    return Runtime(
        orchestrator=orchestrator,
        store=store,
        harness=harness,
        event_bus=bus,
        settings=settings,
        tool_approval=tool_approval,
        router=router,
    )
