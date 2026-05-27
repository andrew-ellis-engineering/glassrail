"""Tests for execution-state types."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from dagagent.core import (
    ExecutionState,
    NodeResult,
    NodeStatus,
    TaskId,
    TaskStatus,
    new_task_id,
)


def test_new_task_id_is_unique_and_sortable() -> None:
    a = new_task_id()
    time.sleep(0.002)
    b = new_task_id()
    assert a != b
    # ULIDs are 26 Crockford-base32 chars and lexicographically time-sortable.
    assert len(a) == 26
    assert len(b) == 26
    assert a < b


def test_execution_state_defaults() -> None:
    state = ExecutionState(task_id=new_task_id(), user_request="hello")
    assert state.status is TaskStatus.PLANNING
    assert state.replan_count == 0
    assert state.results == {}
    assert state.completed_nodes == []
    assert state.skipped_nodes == []
    assert state.branch_log == []
    assert state.final_output is None
    assert state.error is None
    assert state.created_at.tzinfo is UTC
    assert state.updated_at.tzinfo is UTC


def test_touch_advances_updated_at() -> None:
    state = ExecutionState(task_id=new_task_id(), user_request="hi")
    earlier = state.updated_at
    time.sleep(0.002)
    state.touch()
    assert state.updated_at > earlier


def test_node_result_defaults() -> None:
    result = NodeResult(node_id=1, status=NodeStatus.COMPLETED)
    assert result.output is None
    assert result.branch_taken is None
    assert result.confidence == 1.0
    assert result.flagged is False
    assert result.tokens_used == 0
    assert result.execution_time_s == 0.0
    assert result.error is None
    assert result.tier_used is None


def test_execution_state_serialises_with_datetimes() -> None:
    state = ExecutionState(task_id=TaskId("01J0SAMPLEULID0000000000000"), user_request="x")
    dumped = state.model_dump(mode="json")
    # mode="json" serialises datetimes to ISO strings.
    assert isinstance(dumped["created_at"], str)
    assert datetime.fromisoformat(dumped["created_at"]).tzinfo is not None
    restored = ExecutionState.model_validate(dumped)
    assert restored.task_id == state.task_id
    assert restored.status is TaskStatus.PLANNING
