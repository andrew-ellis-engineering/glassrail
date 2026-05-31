"""Typed event types and the in-process event bus.

Every plan, node, branch, and task transition emits a typed Pydantic event
onto the bus. Gateways (SSE, WebSocket, TUI) subscribe via an
``AsyncIterator``. The in-process bus is intended to be swappable for
Redis/NATS later without changing producers.
"""

from __future__ import annotations

from dagagent.events.bus import EventBus, Subscription
from dagagent.events.types import (
    TERMINAL_EVENT_TYPES,
    AwaitingConfirmation,
    BranchDecided,
    Event,
    NodeFinished,
    NodeStarted,
    PlanFailed,
    PlanningStarted,
    PlanReady,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
)

__all__ = [
    "TERMINAL_EVENT_TYPES",
    "AwaitingConfirmation",
    "BranchDecided",
    "Event",
    "EventBus",
    "NodeFinished",
    "NodeStarted",
    "PlanFailed",
    "PlanReady",
    "PlanningStarted",
    "Subscription",
    "TaskCancelled",
    "TaskCompleted",
    "TaskFailed",
]
