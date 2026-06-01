"""Tests for default node prompt invariants."""

from __future__ import annotations

from dagagent.config import prompts


def test_planner_prompt_requests_right_sized_fresh_context_dags() -> None:
    prompt = prompts.DEFAULT_PLANNER_SYSTEM

    assert "right-sized DAG" in prompt
    assert "fresh context" in prompt
    assert "context_needed lists only direct upstream node IDs" in prompt
    assert 'Decision branches must be exactly {"yes": [...], "no": [...]}' in prompt
    assert "final node whose output is the user's answer must be type=result" in prompt
    assert '"format": "concise" | "medium" | "verbose"' in prompt


def test_summary_prompt_prioritizes_downstream_fidelity() -> None:
    prompt = prompts.DEFAULT_SUMMARY_SYSTEM

    assert "downstream consumer" in prompt
    assert "Compress language, not information" in prompt
    assert "Your output will be consumed by" in prompt
    assert "source pointer" in prompt


def test_summary_variant_prompts_have_distinct_roles() -> None:
    assert "concise 1-3 sentence summary" in prompts.SUMMARY_CONCISE_SYSTEM
    assert "thorough summary preserving all key facts" in prompts.SUMMARY_VERBOSE_SYSTEM
    assert prompts.SUMMARY_CONCISE_SYSTEM != prompts.SUMMARY_VERBOSE_SYSTEM


def test_synthesis_and_result_prompts_preserve_caveats_without_inventing() -> None:
    synthesis = prompts.DEFAULT_SYNTHESIS_SYSTEM
    result = prompts.DEFAULT_RESULT_SYSTEM

    assert "Do not introduce facts" in synthesis
    assert "surface the conflict" in synthesis
    assert "final user-facing answer" in synthesis
    assert "Preserve important caveats and uncertainty" in result
    assert "do not invent facts" in result
    # Result must tell the model it is the sole user-visible output
    assert "ONLY text the user will see" in result
    assert "Original user request" in result or "original request" in result


def test_content_prompts_have_confidence_calibration() -> None:
    for prompt in (
        prompts.DEFAULT_THINK_SYSTEM,
        prompts.DEFAULT_SYNTHESIS_SYSTEM,
        prompts.DEFAULT_SUMMARY_SYSTEM,
    ):
        assert "0.9+" in prompt, "calibration anchor missing"
        assert "below 0.3" in prompt, "low-confidence anchor missing"


def test_content_prompts_remind_about_json_string_escaping() -> None:
    for name, prompt in (
        ("think", prompts.DEFAULT_THINK_SYSTEM),
        ("synthesis", prompts.DEFAULT_SYNTHESIS_SYSTEM),
        ("summary", prompts.DEFAULT_SUMMARY_SYSTEM),
        ("result", prompts.DEFAULT_RESULT_SYSTEM),
    ):
        assert "valid JSON string" in prompt, f"{name} prompt missing JSON escape reminder"


def test_planner_prompt_includes_fresh_context_and_args_template_guidance() -> None:
    prompt = prompts.DEFAULT_PLANNER_SYSTEM
    assert "FRESH CONTEXT" in prompt
    assert "context_needed" in prompt
    assert "args_template" in prompt
    # Guidance may span a line break in the literal, so check each fragment.
    assert "leave it null when arguments must come from an" in prompt
    assert "upstream node's output" in prompt


def test_decision_think_and_shape_check_prompts_have_tight_roles() -> None:
    assert "based only on the provided context" in prompts.DEFAULT_DECISION_SYSTEM
    # Decision prompt must not invent facts and must be label-agnostic (labels
    # are passed in the user message, not hard-coded in the system prompt).
    assert "do not invent missing facts" in prompts.DEFAULT_DECISION_SYSTEM
    assert "allowed branch labels" in prompts.DEFAULT_DECISION_SYSTEM
    assert "externally useful reasoning" in prompts.DEFAULT_THINK_SYSTEM
    assert "private scratchpad filler" in prompts.DEFAULT_THINK_SYSTEM
    assert "usable for the node that requested it" in prompts.DEFAULT_SHAPE_CHECK_SYSTEM
    assert "empty-but-valid results" in prompts.DEFAULT_SHAPE_CHECK_SYSTEM
