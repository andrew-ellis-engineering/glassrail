"""Composition root — assemble the agent's collaborators from :class:`Settings`.

One place wires the harness, router, planner, validator, executor, store, and
orchestrator together. The REST gateway and the CLI both build their runtime
here, so the wiring never drifts between entry points.
"""

from __future__ import annotations

from dataclasses import dataclass

from dagagent.config import Settings, get_settings
from dagagent.events import EventBus
from dagagent.executor import Executor, Orchestrator
from dagagent.harness import ToolHarness, register_builtins
from dagagent.planner import Planner
from dagagent.providers import router_from_settings
from dagagent.state import InMemoryStateStore, StateStore
from dagagent.telemetry import configure_tracing
from dagagent.validator import PlanValidator


@dataclass
class Runtime:
    """The assembled collaborators for one running agent."""

    orchestrator: Orchestrator
    store: StateStore
    harness: ToolHarness
    event_bus: EventBus
    settings: Settings


def build_runtime(settings: Settings | None = None, *, store: StateStore | None = None) -> Runtime:
    """Wire a complete agent runtime from settings (defaults to in-memory state)."""
    settings = settings or get_settings()
    configure_tracing(settings)

    bus = EventBus()
    harness = ToolHarness()
    register_builtins(harness)
    router = router_from_settings(settings)
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator)
    executor = Executor(router=router, harness=harness, settings=settings, event_bus=bus)
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
    )
