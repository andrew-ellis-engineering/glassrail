"""StateStore protocol — the persistence interface.

Phase 0.5 ships the task methods that the orchestrator needs. Branch and
memory methods will join this Protocol as those domains come online; both
existing implementations and the Protocol itself extend together.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dagagent.core import ExecutionState, TaskId, TaskStatus


@runtime_checkable
class StateStore(Protocol):
    """Persistence interface every backend implements."""

    # ── Tasks ────────────────────────────────────────────────────────────

    async def save_task(self, state: ExecutionState) -> None:
        """Insert or replace the record for ``state.task_id``."""
        ...

    async def load_task(self, task_id: TaskId) -> ExecutionState | None:
        """Return a copy of the stored state, or ``None`` if absent."""
        ...

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
    ) -> list[ExecutionState]:
        """Return copies of every stored task, optionally filtered by status."""
        ...

    async def delete_task(self, task_id: TaskId) -> bool:
        """Remove a task. Returns ``True`` if it existed, ``False`` otherwise."""
        ...
