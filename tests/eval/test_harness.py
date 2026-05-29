"""Unit tests for the eval harness itself.

These exercise the scoring and reporting logic directly, with hand-built
states — no LLM, no marker, so they run in the ordinary ``uv run pytest``
sweep and guard the harness the eval suite depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dagagent.core import (
    BranchLogEntry,
    ExecutionState,
    Node,
    NodeResult,
    NodeStatus,
    NodeType,
    Plan,
    TaskStatus,
    new_task_id,
)
from dagagent.providers import collect
from tests.eval.harness import (
    EXECUTION,
    PLANNING,
    CheckResult,
    Expectations,
    ScenarioResult,
    format_summary,
    make_scripted_router,
    score_run,
)


def _completed_state() -> ExecutionState:
    """A plausible finished run: tool → result, one branch decision logged."""
    plan = Plan(
        nodes=[
            Node(id=1, type=NodeType.TOOL, description="look up", tool="calendar_get"),
            Node(id=2, type=NodeType.DECISION, description="branch", condition="any?"),
            Node(id=3, type=NodeType.RESULT, description="answer", context_needed=[1]),
        ],
        sorted_node_ids=[1, 2, 3],
    )
    return ExecutionState(
        task_id=new_task_id(),
        user_request="what's on today?",
        plan=plan,
        status=TaskStatus.COMPLETED,
        final_output="You have nothing scheduled today.",
        results={
            1: NodeResult(node_id=1, status=NodeStatus.COMPLETED),
            2: NodeResult(node_id=2, status=NodeStatus.COMPLETED, branch_taken="no"),
            3: NodeResult(node_id=3, status=NodeStatus.COMPLETED),
        },
        branch_log=[
            BranchLogEntry(
                node_id=2,
                condition="any?",
                branch_taken="no",
                confidence=0.9,
                timestamp=datetime.now(UTC),
            )
        ],
    )


def _check(checks: list[CheckResult], name: str) -> bool:
    return next(c.passed for c in checks if c.name == name)


def test_all_expectations_pass() -> None:
    state = _completed_state()
    expect = Expectations(
        min_nodes=3,
        max_nodes=3,
        node_types=(NodeType.TOOL, NodeType.DECISION, NodeType.RESULT),
        tools=("calendar_get",),
        final_output_contains=("nothing scheduled",),
        branches=((2, "no"),),
    )
    checks = score_run(state, expect)
    assert all(c.passed for c in checks)


def test_status_mismatch_fails_only_that_check() -> None:
    state = _completed_state()
    state.status = TaskStatus.FAILED
    checks = score_run(state, Expectations(min_nodes=3, max_nodes=3))
    assert _check(checks, "status_matches") is False
    assert _check(checks, "node_count") is True


def test_node_count_out_of_range_fails() -> None:
    state = _completed_state()
    checks = score_run(state, Expectations(min_nodes=5, max_nodes=10))
    assert _check(checks, "node_count") is False


def test_wrong_branch_fails() -> None:
    state = _completed_state()
    checks = score_run(state, Expectations(branches=((2, "yes"),)))
    assert _check(checks, "branches_taken") is False


def test_missing_substring_fails() -> None:
    state = _completed_state()
    checks = score_run(state, Expectations(final_output_contains=("dentist",)))
    assert _check(checks, "final_output_contains") is False


def test_failed_node_trips_no_failed_nodes() -> None:
    state = _completed_state()
    state.results[3] = NodeResult(node_id=3, status=NodeStatus.FAILED, error="boom")
    checks = score_run(state, Expectations())
    assert _check(checks, "no_failed_nodes") is False


def test_content_false_drops_script_specific_checks() -> None:
    state = _completed_state()
    expect = Expectations(final_output_contains=("anything",), branches=((2, "yes"),))
    names = {c.name for c in score_run(state, expect, content=False)}
    assert "final_output_contains" not in names
    assert "branches_taken" not in names


def test_planning_failure_scores_only_status() -> None:
    state = ExecutionState(
        task_id=new_task_id(),
        user_request="garbled",
        status=TaskStatus.FAILED,
        error="Planning failed after 2 attempts",
    )
    checks = score_run(state, Expectations(status=TaskStatus.FAILED, must_validate=False))
    assert [c.name for c in checks] == ["status_matches"]
    assert checks[0].passed is True


def test_scenario_result_scoring_math() -> None:
    result = ScenarioResult(
        scenario_id="demo",
        checks=[
            CheckResult("a", PLANNING, True),
            CheckResult("b", PLANNING, False),
            CheckResult("c", EXECUTION, True),
            CheckResult("d", EXECUTION, True),
        ],
        threshold=0.75,
    )
    assert result.score == 0.75
    assert result.dimension_score(PLANNING) == 0.5
    assert result.dimension_score(EXECUTION) == 1.0
    assert result.passed is True
    assert {c.name for c in result.failures()} == {"b"}


def test_dimension_score_none_when_absent() -> None:
    result = ScenarioResult(scenario_id="empty", checks=[])
    assert result.dimension_score(PLANNING) is None
    assert result.score == 0.0
    assert result.passed is False


def test_format_summary_renders_ids_and_verdicts() -> None:
    results = [
        ScenarioResult("good", [CheckResult("x", EXECUTION, True)], threshold=1.0),
        ScenarioResult("bad", [CheckResult("y", PLANNING, False, "nope")], threshold=1.0),
    ]
    text = format_summary(results)
    assert "good" in text and "bad" in text
    assert "PASS" in text and "FAIL" in text
    assert "1/2 passed" in text
    assert "nope" in text  # failure detail is surfaced


async def test_scripted_router_shares_queue_across_tiers() -> None:
    """Responses pop in global call order regardless of which tier serves."""
    router, queue = make_scripted_router(["first", "second", "third"])
    out_t2, _ = await collect(router.complete([], min_tier=2))
    out_t0, _ = await collect(router.complete([], min_tier=0))
    out_t0b, _ = await collect(router.complete([], min_tier=0))
    assert (out_t2, out_t0, out_t0b) == ("first", "second", "third")
    assert len(queue) == 0
