"""Tests for the typed events and the in-process EventBus."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from glassrail.core import NodeStatus, new_task_id
from glassrail.events import (
    EventBus,
    NodeFinished,
    PlanningStarted,
    PlanReady,
)

_TID = new_task_id()


async def _next(sub: object) -> object:
    # anext with a timeout so a missed delivery fails fast instead of hanging.
    return await asyncio.wait_for(anext(sub), 1.0)  # type: ignore[call-overload]


async def test_subscriber_receives_events_in_order() -> None:
    bus = EventBus()
    async with bus.subscribe() as sub:
        await bus.publish(PlanningStarted(task_id=_TID))
        await bus.publish(PlanReady(task_id=_TID, node_count=3))
        first = await _next(sub)
        second = await _next(sub)

    assert isinstance(first, PlanningStarted)
    assert isinstance(second, PlanReady)
    assert second.node_count == 3


async def test_events_before_subscribe_are_not_delivered() -> None:
    bus = EventBus()
    # No subscribers yet — this event is dropped on the floor.
    await bus.publish(PlanningStarted(task_id=_TID))

    async with bus.subscribe() as sub:
        await bus.publish(PlanReady(task_id=_TID, node_count=1))
        received = await _next(sub)

    assert isinstance(received, PlanReady)


async def test_fan_out_to_multiple_subscribers() -> None:
    bus = EventBus()
    async with bus.subscribe() as a, bus.subscribe() as b:
        assert bus.subscriber_count == 2
        await bus.publish(PlanningStarted(task_id=_TID))
        ea = await _next(a)
        eb = await _next(b)

    assert isinstance(ea, PlanningStarted)
    assert isinstance(eb, PlanningStarted)


async def test_unsubscribe_on_context_exit() -> None:
    bus = EventBus()
    async with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


async def test_slow_subscriber_drops_oldest_events() -> None:
    bus = EventBus(max_queue=2)
    async with bus.subscribe() as sub:
        for i in range(4):
            await bus.publish(PlanReady(task_id=_TID, node_count=i))
        # Queue capacity is 2; node_counts 0 and 1 were evicted.
        got = [await _next(sub), await _next(sub)]
        dropped = sub.dropped

    counts = [e.node_count for e in got if isinstance(e, PlanReady)]
    assert counts == [2, 3]
    assert dropped == 2


async def test_slow_subscriber_logs_drop_warning(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus(max_queue=1)
    with caplog.at_level(logging.WARNING, logger="glassrail.events.bus"):
        async with bus.subscribe() as sub:
            await bus.publish(PlanReady(task_id=_TID, node_count=1))
            await bus.publish(PlanReady(task_id=_TID, node_count=2))
            assert sub.dropped == 1

    assert "EventBus subscription dropped events" in caplog.text
    assert caplog.records
    assert caplog.records[0].__dict__["dropped"] == 1


async def test_task_scoped_subscription_ignores_other_tasks() -> None:
    bus = EventBus(max_queue=1)
    other = new_task_id()
    async with bus.subscribe(task_id=_TID) as sub:
        await bus.publish(PlanningStarted(task_id=other))
        await bus.publish(PlanningStarted(task_id=other))
        await bus.publish(PlanReady(task_id=_TID, node_count=7))
        received = await _next(sub)

    assert isinstance(received, PlanReady)
    assert received.task_id == _TID
    assert received.node_count == 7
    assert sub.dropped == 0


def test_event_serialises_with_type_discriminator() -> None:
    event = NodeFinished(
        task_id=_TID,
        node_id=2,
        status=NodeStatus.COMPLETED,
        confidence=0.9,
        flagged=False,
        tier_used=0,
    )
    data = json.loads(event.model_dump_json())
    assert data["type"] == "node_finished"
    assert data["status"] == "completed"
    assert data["node_id"] == 2
    assert data["task_id"] == _TID
