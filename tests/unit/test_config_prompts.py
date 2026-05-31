"""Tests for default node prompt invariants."""

from __future__ import annotations

from dagagent.config import prompts


def test_planner_prompt_requests_right_sized_fresh_context_dags() -> None:
    prompt = prompts.DEFAULT_PLANNER_SYSTEM

    assert "right-sized DAG" in prompt
    assert "fresh context" in prompt
    assert "context_needed lists only direct upstream node IDs" in prompt
    assert 'Decision branches must be exactly {"yes": [...], "no": [...]}' in prompt
    assert "normally have one result node" in prompt


def test_summary_prompt_prioritizes_downstream_fidelity() -> None:
    prompt = prompts.DEFAULT_SUMMARY_SYSTEM

    assert "downstream consumer" in prompt
    assert "Compress language, not information" in prompt
    assert "Your output will be consumed by" in prompt
    assert "source pointer" in prompt


def test_synthesis_and_result_prompts_preserve_caveats_without_inventing() -> None:
    synthesis = prompts.DEFAULT_SYNTHESIS_SYSTEM
    result = prompts.DEFAULT_RESULT_SYSTEM

    assert "Do not introduce facts" in synthesis
    assert "surface the conflict" in synthesis
    assert "final user-facing answer" in synthesis
    assert "Preserve important caveats and uncertainty" in result
    assert "do not invent facts" in result


def test_decision_think_and_shape_check_prompts_have_tight_roles() -> None:
    assert "based only on the provided context" in prompts.DEFAULT_DECISION_SYSTEM
    assert "not supported" in prompts.DEFAULT_DECISION_SYSTEM
    assert "externally useful reasoning" in prompts.DEFAULT_THINK_SYSTEM
    assert "private scratchpad filler" in prompts.DEFAULT_THINK_SYSTEM
    assert "usable for the node that requested it" in prompts.DEFAULT_SHAPE_CHECK_SYSTEM
    assert "empty-but-valid results" in prompts.DEFAULT_SHAPE_CHECK_SYSTEM
