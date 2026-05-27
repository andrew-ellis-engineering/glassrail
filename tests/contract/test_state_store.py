"""Contract tests every StateStore implementation must pass.

Add new backends to the ``STORE_FACTORIES`` parametrisation; they'll be
exercised against the full contract automatically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from dagagent.core import ExecutionState, TaskStatus, new_task_id
from dagagent.state import InMemoryStateStore, StateStore

StoreFactory = Callable[[], Awaitable[StateStore]]


async def _new_in_memory() -> StateStore:
    return InMemoryStateStore()


STORE_FACTORIES: list[tuple[str, StoreFactory]] = [
    ("in_memory", _new_in_memory),
]


@pytest.fixture(params=[f for _, f in STORE_FACTORIES], ids=[n for n, _ in STORE_FACTORIES])
async def store(request: pytest.FixtureRequest) -> StateStore:
    factory: StoreFactory = request.param
    return await factory()


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
