"""SQLite-backed StateStore implementation.

Durable, single-file persistence built on ``aiosqlite``. Each task is stored
as one row keyed by ``task_id`` with the full ``ExecutionState`` serialised
as JSON; ``status`` is a denormalised column so list-by-status can use an
index instead of scanning every row.

Install the optional dependency to use this backend::

    pip install "glassrail[sqlite]"
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from glassrail.core import ExecutionState, TaskId, TaskStatus

if TYPE_CHECKING:
    from types import TracebackType


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status  TEXT NOT NULL,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status);
"""


class SqliteStateStore:
    """``aiosqlite``-backed StateStore. Durable across process restarts.

    The store owns a long-lived connection opened on first use. Callers
    should ``await store.close()`` when shutting down, or use the store as
    an async context manager.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            conn = await aiosqlite.connect(self._path)
            await conn.executescript(_SCHEMA)
            await conn.commit()
            self._conn = conn
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> SqliteStateStore:
        await self._connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ── StateStore protocol ──────────────────────────────────────────────

    async def save_task(self, state: ExecutionState) -> None:
        conn = await self._connect()
        await conn.execute(
            "INSERT OR REPLACE INTO tasks(task_id, status, data) VALUES (?, ?, ?)",
            (state.task_id, state.status.value, state.model_dump_json()),
        )
        await conn.commit()

    async def load_task(self, task_id: TaskId) -> ExecutionState | None:
        conn = await self._connect()
        async with conn.execute("SELECT data FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return ExecutionState.model_validate_json(row[0])

    async def transition_task_status(
        self,
        task_id: TaskId,
        *,
        from_statuses: Collection[TaskStatus],
        to_status: TaskStatus,
    ) -> ExecutionState | None:
        allowed = tuple(from_statuses)
        if not allowed:
            return None

        conn = await self._connect()
        async with conn.execute("SELECT data FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None

        state = ExecutionState.model_validate_json(row[0])
        if state.status not in allowed:
            return None
        state.status = to_status
        state.touch()

        placeholders = ", ".join("?" for _ in allowed)
        query = (
            "UPDATE tasks SET status = ?, data = ? "
            f"WHERE task_id = ? AND status IN ({placeholders})"
        )
        params = (
            to_status.value,
            state.model_dump_json(),
            task_id,
            *(status.value for status in allowed),
        )
        async with conn.execute(query, params) as cursor:
            transitioned = cursor.rowcount > 0
        await conn.commit()
        return state if transitioned else None

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
    ) -> list[ExecutionState]:
        conn = await self._connect()
        if status is None:
            cursor = await conn.execute("SELECT data FROM tasks")
        else:
            cursor = await conn.execute("SELECT data FROM tasks WHERE status = ?", (status.value,))
        async with cursor:
            rows = await cursor.fetchall()
        return [ExecutionState.model_validate_json(row[0]) for row in rows]

    async def delete_task(self, task_id: TaskId) -> bool:
        conn = await self._connect()
        cursor = await conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        await conn.commit()
        return cursor.rowcount > 0
