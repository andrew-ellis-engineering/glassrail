"""Tests for planner cookbook recipe loading and selection."""

from __future__ import annotations

from glassrail.planner.cookbook import PlannerCookbook


def test_default_cookbook_loads_bundled_recipes() -> None:
    cookbook = PlannerCookbook.load_default()
    ids = {recipe.id for recipe in cookbook.recipes}

    assert "direct_answer" in ids
    assert "single_tool" in ids
    assert "web_research" in ids
    assert "conditional_branch" in ids


def test_cookbook_selects_web_recipe_for_search_request() -> None:
    cookbook = PlannerCookbook.load_default()

    recipe = cookbook.select(
        request="Do a web search for the Raft consensus algorithm",
        tool_names={"file_read", "web_search"},
    )

    assert recipe.id == "web_research"


def test_cookbook_selects_single_tool_for_named_tool_capability() -> None:
    cookbook = PlannerCookbook.load_default()

    recipe = cookbook.select(
        request="Read the project README and summarize it",
        tool_names={"file_read", "calendar_get"},
    )

    assert recipe.id == "single_tool"


def test_cookbook_selects_top_k_ranked_recipes() -> None:
    cookbook = PlannerCookbook.load_default()

    recipes = cookbook.select_many(
        request="Research and compare Raft and Paxos using web sources",
        tool_names={"web_search", "file_read"},
        k=3,
    )

    assert len(recipes) == 3
    assert recipes[0].id in {"compare_aggregate", "web_research"}
    assert "web_research" in {recipe.id for recipe in recipes}
    assert "compare_aggregate" in {recipe.id for recipe in recipes}


def test_cookbook_prefers_conditional_for_obvious_binary_branch() -> None:
    cookbook = PlannerCookbook.load_default()

    recipe = cookbook.select(
        request=(
            "Decide whether the record is active or archived. If active, "
            "write the live-message; otherwise write the archive-message."
        ),
        tool_names=set(),
    )

    assert recipe.id == "conditional_branch"


def test_cookbook_selects_compare_for_recommendation_axes() -> None:
    cookbook = PlannerCookbook.load_default()

    recipe = cookbook.select(
        request=(
            "Compare option A and option B across reliability, cost, and trade-offs, "
            "then recommend one."
        ),
        tool_names=set(),
    )

    assert recipe.id == "compare_aggregate"


def test_cookbook_selects_direct_answer_for_logic_puzzle() -> None:
    cookbook = PlannerCookbook.load_default()

    recipe = cookbook.select(
        request=(
            "Use logic and deduction to solve a three-person assignment puzzle from "
            "the provided constraints."
        ),
        tool_names=set(),
    )

    assert recipe.id == "direct_answer"


def test_cookbook_prompt_says_to_adapt_ranked_candidates() -> None:
    cookbook = PlannerCookbook.load_default()

    prompt = cookbook.to_prompt(
        request="Compare Raft and Paxos",
        tool_names={"file_read"},
    )

    assert "best-effort heuristic" in prompt
    assert "scaffolds, not templates" in prompt
    assert "Compare nearby shapes" in prompt
    assert "Never copy the skeleton verbatim" in prompt
    assert "right-sized" in prompt
    assert "include every node needed for correctness" in prompt
    assert "Top candidate recipes:" in prompt
    assert "Candidate 1: compare_aggregate" in prompt
    assert "Candidate 2:" in prompt
    assert "I recommend <option>" in prompt
    assert "Name the requested candidates and axes" in prompt
