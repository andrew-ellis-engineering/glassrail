"""In-memory StateStore implementation.

Suitable for tests and ephemeral single-process runs. Loses everything on
process restart — use the SQLite backend for any deployment that needs to
survive a restart.

Every read and write deep-copies the stored ``ExecutionState`` so callers
cannot accidentally mutate each other's view, mirroring what a real
database-backed implementation provides.
"""

from __future__ import annotations

from collections.abc import Collection

from glassrail.core import ExecutionState, TaskId, TaskStatus


class InMemoryStateStore:
    """Dict-backed StateStore. Process-local; non-durable."""

    def __init__(self) -> None:
        self._tasks: dict[TaskId, ExecutionState] = {}

    async def save_task(self, state: ExecutionState) -> None:
        self._tasks[state.task_id] = state.model_copy(deep=True)

    async def load_task(self, task_id: TaskId) -> ExecutionState | None:
        stored = self._tasks.get(task_id)
        return stored.model_copy(deep=True) if stored is not None else None

    async def transition_task_status(
        self,
        task_id: TaskId,
        *,
        from_statuses: Collection[TaskStatus],
        to_status: TaskStatus,
    ) -> ExecutionState | None:
        stored = self._tasks.get(task_id)
        if stored is None or stored.status not in from_statuses:
            return None
        updated = stored.model_copy(deep=True)
        updated.status = to_status
        updated.touch()
        self._tasks[task_id] = updated.model_copy(deep=True)
        return updated

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
    ) -> list[ExecutionState]:
        states = self._tasks.values()
        if status is not None:
            states = [s for s in states if s.status is status]
        return [s.model_copy(deep=True) for s in states]

    async def delete_task(self, task_id: TaskId) -> bool:
        return self._tasks.pop(task_id, None) is not None
