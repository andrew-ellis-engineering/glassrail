"""Contract tests every StateStore implementation must pass.

Add new backends to the ``STORE_FACTORIES`` parametrisation; they'll be
exercised against the full contract automatically.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from glassrail.core import ExecutionState, TaskStatus, new_task_id
from glassrail.state import InMemoryStateStore, StateStore
from glassrail.state.sqlite import SqliteStateStore

StoreFactory = Callable[[Path], Awaitable[StateStore]]


async def _new_in_memory(_tmp: Path) -> StateStore:
    return InMemoryStateStore()


async def _new_sqlite(tmp: Path) -> StateStore:
    return SqliteStateStore(tmp / "store.sqlite")


STORE_FACTORIES: list[tuple[str, StoreFactory]] = [
    ("in_memory", _new_in_memory),
    ("sqlite", _new_sqlite),
]


@pytest.fixture(params=[f for _, f in STORE_FACTORIES], ids=[n for n, _ in STORE_FACTORIES])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[StateStore]:
    factory: StoreFactory = request.param
    instance = await factory(tmp_path)
    try:
        yield instance
    finally:
        if isinstance(instance, SqliteStateStore):
            await instance.close()


def _make_state(*, request: str = "do a thing") -> ExecutionState:
    return ExecutionState(task_id=new_task_id(), user_request=request)


async def test_save_and_load_round_trip(store: StateStore) -> None:
    state = _make_state(request="hello")
    await store.save_task(state)
    loaded = await store.load_task(state.task_id)
    assert loaded is not None
    assert loaded.task_id == state.task_id
    assert loaded.user_request == "hello"


async def test_load_missing_returns_none(store: StateStore) -> None:
    assert await store.load_task(new_task_id()) is None


async def test_status_transition_updates_only_allowed_state(store: StateStore) -> None:
    state = _make_state()
    state.status = TaskStatus.PAUSED
    await store.save_task(state)

    transitioned = await store.transition_task_status(
        state.task_id,
        from_statuses=(TaskStatus.PAUSED,),
        to_status=TaskStatus.RESUMING,
    )

    assert transitioned is not None
    assert transitioned.status is TaskStatus.RESUMING
    assert transitioned.updated_at > state.updated_at
    stored = await store.load_task(state.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.RESUMING


async def test_status_transition_rejects_disallowed_state(store: StateStore) -> None:
    state = _make_state()
    await store.save_task(state)

    transitioned = await store.transition_task_status(
        state.task_id,
        from_statuses=(TaskStatus.PAUSED,),
        to_status=TaskStatus.RESUMING,
    )

    assert transitioned is None
    stored = await store.load_task(state.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.PLANNING


async def test_status_transition_has_single_winner(store: StateStore) -> None:
    state = _make_state()
    state.status = TaskStatus.PAUSED
    await store.save_task(state)

    results = await asyncio.gather(
        *(
            store.transition_task_status(
                state.task_id,
                from_statuses=(TaskStatus.PAUSED,),
                to_status=TaskStatus.RESUMING,
            )
            for _ in range(8)
        )
    )

    assert sum(result is not None for result in results) == 1


async def test_sqlite_status_transition_has_single_cross_connection_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared.sqlite"
    first = SqliteStateStore(path)
    second = SqliteStateStore(path)
    state = _make_state()
    state.status = TaskStatus.PAUSED
    try:
        await first.save_task(state)
        assert await second.load_task(state.task_id) is not None
        results = await asyncio.gather(
            first.transition_task_status(
                state.task_id,
                from_statuses=(TaskStatus.PAUSED,),
                to_status=TaskStatus.RESUMING,
            ),
            second.transition_task_status(
                state.task_id,
                from_statuses=(TaskStatus.PAUSED,),
                to_status=TaskStatus.RESUMING,
            ),
        )
    finally:
        await first.close()
        await second.close()

    assert sum(result is not None for result in results) == 1


async def test_save_replaces_existing(store: StateStore) -> None:
    state = _make_state(request="v1")
    await store.save_task(state)
    state.user_request = "v2"
    await store.save_task(state)

    loaded = await store.load_task(state.task_id)
    assert loaded is not None
    assert loaded.user_request == "v2"


async def test_list_returns_all_then_filters_by_status(store: StateStore) -> None:
    a = _make_state(request="a")
    b = _make_state(request="b")
    c = _make_state(request="c")
    b.status = TaskStatus.COMPLETED
    c.status = TaskStatus.COMPLETED

    await store.save_task(a)
    await store.save_task(b)
    await store.save_task(c)

    all_states = await store.list_tasks()
    assert {s.task_id for s in all_states} == {a.task_id, b.task_id, c.task_id}

    completed = await store.list_tasks(status=TaskStatus.COMPLETED)
    assert {s.task_id for s in completed} == {b.task_id, c.task_id}


async def test_delete_returns_true_when_present(store: StateStore) -> None:
    state = _make_state()
    await store.save_task(state)
    assert await store.delete_task(state.task_id) is True
    assert await store.load_task(state.task_id) is None


async def test_delete_returns_false_when_absent(store: StateStore) -> None:
    assert await store.delete_task(new_task_id()) is False


async def test_load_returns_isolated_copy(store: StateStore) -> None:
    """Mutating a loaded state must not affect the stored record."""
    state = _make_state(request="original")
    await store.save_task(state)

    loaded = await store.load_task(state.task_id)
    assert loaded is not None
    loaded.user_request = "mutated"

    reloaded = await store.load_task(state.task_id)
    assert reloaded is not None
    assert reloaded.user_request == "original"


async def test_save_isolates_caller_object(store: StateStore) -> None:
    """Mutating the input after save must not affect the stored record."""
    state = _make_state(request="original")
    await store.save_task(state)
    state.user_request = "mutated"

    loaded = await store.load_task(state.task_id)
    assert loaded is not None
    assert loaded.user_request == "original"


def test_in_memory_satisfies_protocol() -> None:
    assert isinstance(InMemoryStateStore(), StateStore)


def test_sqlite_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(SqliteStateStore(tmp_path / "p.sqlite"), StateStore)
