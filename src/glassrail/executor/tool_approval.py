"""In-process broker for per-tool human approval."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from glassrail.core import TaskId, ToolRisk
from glassrail.events import EventBus, ToolApprovalRequested


@dataclass(frozen=True)
class ToolApprovalRequest:
    """A concrete tool invocation awaiting approval."""

    approval_id: str
    task_id: TaskId
    node_id: int
    tool_name: str
    risk: ToolRisk
    args: dict[str, object]
    description: str


class ToolApprovalBroker:
    """Coordinates executor-side approval waits with an interactive gateway."""

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._always_allow: set[str] = set()

    def is_always_allowed(self, tool_name: str) -> bool:
        return tool_name in self._always_allow

    def remember_allow(self, tool_name: str) -> None:
        self._always_allow.add(tool_name)

    async def request(
        self,
        *,
        task_id: TaskId,
        node_id: int,
        tool_name: str,
        risk: ToolRisk,
        args: dict[str, object],
        description: str,
    ) -> bool:
        approval_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[approval_id] = future
        await self._bus.publish(
            ToolApprovalRequested(
                task_id=task_id,
                approval_id=approval_id,
                node_id=node_id,
                tool_name=tool_name,
                risk=risk,
                args=args,
                description=description,
            )
        )
        try:
            return await future
        finally:
            self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool) -> None:
        future = self._pending.get(approval_id)
        if future is not None and not future.done():
            future.set_result(approved)

    def cancel(self, approval_id: str) -> None:
        self.resolve(approval_id, False)


def as_jsonable_args(args: dict[str, object]) -> dict[str, Any]:
    """Best-effort JSON-ish copy for permission payloads/events."""
    return {str(k): v for k, v in args.items()}
