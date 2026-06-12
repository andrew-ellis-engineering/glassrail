"""Tests for default node prompt invariants."""

from __future__ import annotations

from glassrail.config import prompts


def test_planner_prompt_requests_right_sized_fresh_context_dags() -> None:
    prompt = prompts.DEFAULT_PLANNER_SYSTEM

    assert "right-sized DAG" in prompt
    assert "fresh context" in prompt
    assert "context_needed lists only direct upstream node IDs" in prompt
    assert 'Decision branches must be exactly {"yes": [...], "no": [...]}' in prompt
    assert "final node whose output is the user's answer must be type=result" in prompt
    assert '"format": "concise" | "medium" | "verbose"' in prompt


def test_planner_prompt_shows_correct_subplan_tool_shape() -> None:
    prompt = prompts.DEFAULT_PLANNER_SYSTEM

    assert '"type": "tool", "tool": "file_read"' in prompt
    assert '"type": "file_read"' in prompt
    assert '"file_read" is a tool name, not a node type' in prompt
    assert 'If the limit says "At most 2' in prompt
    assert "three sibling subplan nodes is invalid" in prompt


def test_planner_prompt_prevents_vague_rejection_and_unregistered_tools() -> None:
    prompt = prompts.DEFAULT_PLANNER_SYSTEM

    assert "Use ONLY tool names that appear in the Available tools list" in prompt
    assert "Optional web" in prompt
    assert 'BAD: {"rejection":"The request is too vague"}' in prompt
    assert "Every node description must be a non-empty string" in prompt
    assert 'recommend", "best fit"' in prompt
    assert "put named-person and planted-fact preservation" in prompt
    assert "final result description must name every comparison" in prompt
    assert "Every decision node also needs a non-empty" in prompt
    assert "binary-dependent or category-dependent answer" in prompt
    assert "Branch result descriptions must include both the branch/category label" in prompt
    assert "Logic puzzles and constraint-elimination tasks" in prompt
    assert "name every candidate" in prompt
    assert "Copy every load-bearing fact" in prompt
    assert "numbers, units, formulas, named candidates" in prompt
    assert "Copy source-of-knowledge instructions" in prompt
    assert "stable/general knowledge is enough" in prompt
    assert "at least one concise sentence per candidate or category" in prompt
    assert "final answer should be prose, not a raw object" in prompt
    assert "For closed-book comparison tasks with sibling evaluation nodes" in prompt
    assert "no upstream context must not say information is missing" in prompt


def test_summary_prompt_prioritizes_downstream_fidelity() -> None:
    prompt = prompts.DEFAULT_SUMMARY_SYSTEM

    assert "downstream consumer" in prompt
    assert "Compress language, not information" in prompt
    assert "Your output will be consumed by" in prompt
    assert "source pointer" in prompt
    assert "include that full name even under tight bullet limits" in prompt


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
    assert "stable general knowledge" in synthesis
    assert "Preserve important caveats and uncertainty" in result
    assert "do not invent facts" in result
    assert "stable general knowledge" in result
    assert "I recommend <option>" in result
    assert "preserve every named candidate" in result
    assert "comparison axis" in result
    assert "trade-off" in result
    assert "winner-only answer" in result
    assert "plain prose with units" in result
    assert "logic or deduction tasks" in result
    assert "preserve that conclusion" in result
    assert "do not replace it with a different final answer" in result
    assert "classification/branch choice and a branch-specific value" in result
    assert "at least one concise sentence about each candidate or category" in result
    assert "write the final answer as prose rather than a raw JSON object" in result
    assert 'Do not introduce it with "I recommend"' in result
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
    assert "stable general knowledge" in prompts.DEFAULT_THINK_SYSTEM
    assert "usable for the node that requested it" in prompts.DEFAULT_SHAPE_CHECK_SYSTEM
    assert "empty-but-valid results" in prompts.DEFAULT_SHAPE_CHECK_SYSTEM


def test_runtime_prompts_avoid_visible_eval_task_vocabulary() -> None:
    combined = "\n".join(
        (
            prompts.DEFAULT_PLANNER_SYSTEM,
            prompts.DEFAULT_DECISION_SYSTEM,
            prompts.DEFAULT_THINK_SYSTEM,
            prompts.DEFAULT_SYNTHESIS_SYSTEM,
            prompts.DEFAULT_SUMMARY_SYSTEM,
            prompts.SUMMARY_CONCISE_SYSTEM,
            prompts.SUMMARY_VERBOSE_SYSTEM,
            prompts.DEFAULT_RESULT_SYSTEM,
        )
    ).lower()

    forbidden = (
        "even/odd",
        "northern/southern",
        "hemisphere",
        "alice/bob/carol",
        "tcp vs udp",
        "postgresql",
        "clickhouse",
        "duckdb",
        "druid",
    )
    for term in forbidden:
        assert term not in combined
