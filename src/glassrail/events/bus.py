"""In-process async event bus.

A single :class:`EventBus` fans every published :class:`Event` out to all
active subscribers. Each subscriber owns a bounded queue; a slow consumer
drops its own oldest events rather than blocking producers. The bus is
intentionally minimal so it can be swapped for Redis/NATS later without
changing producers or consumers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType

from glassrail.core import TaskId
from glassrail.events.types import Event

# A registry of live subscriptions. The bus owns the set; each Subscription
# adds and removes itself across its context lifetime.
_SubscriptionRegistry = set["Subscription"]

log = logging.getLogger(__name__)


class Subscription:
    """A single consumer's view of the bus.

    Use as an async context manager so registration and cleanup are
    automatic, then iterate it::

        async with bus.subscribe() as events:
            async for event in events:
                ...
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[Event],
        registry: _SubscriptionRegistry,
        task_id: TaskId | None,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._task_id = task_id
        self._dropped = 0
        self._last_drop_warning_s = 0.0

    async def __aenter__(self) -> Subscription:
        self._registry.add(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._registry.discard(self)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()

    @property
    def dropped(self) -> int:
        """Number of events evicted from this subscription's queue."""
        return self._dropped

    def deliver(self, event: Event) -> None:
        if self._task_id is not None and event.task_id != self._task_id:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            _ = self._queue.get_nowait()
            self._queue.put_nowait(event)
            self._dropped += 1
            self._log_drop()

    def _log_drop(self) -> None:
        now = time.monotonic()
        if now - self._last_drop_warning_s < 10.0:
            return
        self._last_drop_warning_s = now
        log.warning(
            "EventBus subscription dropped events",
            extra={
                "task_id": self._task_id,
                "dropped": self._dropped,
            },
        )


class EventBus:
    """Fans published events out to every active subscriber."""

    def __init__(self, *, max_queue: int = 1000) -> None:
        self._registry: _SubscriptionRegistry = set()
        self._max_queue = max_queue

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to every current subscriber.

        Async by signature (so a future network-backed bus is a drop-in)
        even though the in-process implementation never awaits.
        """
        for subscription in list(self._registry):
            subscription.deliver(event)

    def subscribe(self, *, task_id: TaskId | None = None) -> Subscription:
        """Return a new, not-yet-registered :class:`Subscription`.

        Registration happens on ``__aenter__``; events published before
        then are not delivered to this subscriber.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max_queue)
        return Subscription(queue=queue, registry=self._registry, task_id=task_id)

    @property
    def subscriber_count(self) -> int:
        return len(self._registry)
