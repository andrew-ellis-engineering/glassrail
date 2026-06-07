"""In-process async event bus.

A single :class:`EventBus` fans every published :class:`Event` out to all
active subscribers. Each subscriber owns a bounded queue; a slow consumer
drops its own oldest events rather than blocking producers. The bus is
intentionally minimal so it can be swapped for Redis/NATS later without
changing producers or consumers.
"""

from __future__ import annotations

import asyncio
from types import TracebackType

from glassrail.events.types import Event

# A registry of live subscriber queues. The bus owns the set; each
# Subscription adds and removes its own queue across its context lifetime.
_QueueRegistry = set["asyncio.Queue[Event]"]


def _deliver(queue: asyncio.Queue[Event], event: Event) -> None:
    """Enqueue an event, dropping the oldest if the queue is full.

    A slow subscriber sheds its own backlog instead of applying
    backpressure to producers (which would stall the whole task pipeline).
    """
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        _ = queue.get_nowait()
        queue.put_nowait(event)


class Subscription:
    """A single consumer's view of the bus.

    Use as an async context manager so registration and cleanup are
    automatic, then iterate it::

        async with bus.subscribe() as events:
            async for event in events:
                ...
    """

    def __init__(self, *, queue: asyncio.Queue[Event], registry: _QueueRegistry) -> None:
        self._queue = queue
        self._registry = registry

    async def __aenter__(self) -> Subscription:
        self._registry.add(self._queue)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._registry.discard(self._queue)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()


class EventBus:
    """Fans published events out to every active subscriber."""

    def __init__(self, *, max_queue: int = 1000) -> None:
        self._registry: _QueueRegistry = set()
        self._max_queue = max_queue

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to every current subscriber.

        Async by signature (so a future network-backed bus is a drop-in)
        even though the in-process implementation never awaits.
        """
        for queue in list(self._registry):
            _deliver(queue, event)

    def subscribe(self) -> Subscription:
        """Return a new, not-yet-registered :class:`Subscription`.

        Registration happens on ``__aenter__``; events published before
        then are not delivered to this subscriber.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max_queue)
        return Subscription(queue=queue, registry=self._registry)

    @property
    def subscriber_count(self) -> int:
        return len(self._registry)
