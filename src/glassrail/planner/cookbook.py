"""Structured planner cookbook support.

Recipes are bundled JSON files that describe reusable plan shapes. They are
examples, not templates: the planner prompt explicitly asks the model to adapt
the candidate recipe to the user's request and currently registered tools.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, Self, cast


@dataclass(frozen=True)
class CookbookRecipe:
    """One reusable planning recipe loaded from JSON."""

    id: str
    title: str
    description: str
    keywords: tuple[str, ...]
    when_to_use: tuple[str, ...]
    skeleton: tuple[str, ...]
    adaptation_notes: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> Self:
        return cls(
            id=str(raw["id"]),
            title=str(raw["title"]),
            description=str(raw["description"]),
            keywords=_string_tuple(raw.get("keywords", [])),
            when_to_use=_string_tuple(raw.get("when_to_use", [])),
            skeleton=_string_tuple(raw.get("skeleton", [])),
            adaptation_notes=_string_tuple(raw.get("adaptation_notes", [])),
        )

    def to_prompt(self, *, heading: str = "Selected recipe") -> str:
        """Render this recipe for the planner prompt."""
        return "\n".join(
            [
                f"{heading}: {self.id} — {self.title}",
                f"Description: {self.description}",
                "When to use:",
                *[f"- {item}" for item in self.when_to_use],
                "Adaptable skeleton:",
                *[f"- {item}" for item in self.skeleton],
                "Adaptation notes:",
                *[f"- {item}" for item in self.adaptation_notes],
            ]
        )


class PlannerCookbook:
    """Small recipe registry with deterministic request-time selection."""

    def __init__(self, recipes: list[CookbookRecipe]) -> None:
        if not recipes:
            raise ValueError("PlannerCookbook requires at least one recipe")
        self._recipes = sorted(recipes, key=lambda recipe: recipe.id)

    @classmethod
    def load_default(cls) -> Self:
        root = resources.files("glassrail.planner.cookbooks")
        recipes: list[CookbookRecipe] = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if not child.name.endswith(".json"):
                continue
            raw = json.loads(child.read_text(encoding="utf-8"))
            recipes.append(CookbookRecipe.from_mapping(cast("dict[str, Any]", raw)))
        return cls(recipes)

    @property
    def recipes(self) -> tuple[CookbookRecipe, ...]:
        return tuple(self._recipes)

    def select(self, *, request: str, tool_names: set[str]) -> CookbookRecipe:
        """Choose one likely-useful recipe without treating it as mandatory."""
        return self.select_many(request=request, tool_names=tool_names, k=1)[0]

    def select_many(
        self,
        *,
        request: str,
        tool_names: set[str],
        k: int = 3,
    ) -> tuple[CookbookRecipe, ...]:
        """Choose the top-k likely-useful recipes for one planner prompt."""
        if k < 1:
            raise ValueError("k must be at least 1")
        request_lower = request.lower()
        ranked = sorted(
            self._recipes,
            key=lambda recipe: (
                _recipe_score(recipe, request_lower=request_lower, tool_names=tool_names),
                recipe.id,
            ),
            reverse=True,
        )
        return tuple(ranked[: min(k, len(ranked))])

    def to_prompt(self, *, request: str, tool_names: set[str], k: int = 3) -> str:
        """Render cookbook guidance and ranked candidate recipes."""
        selected = self.select_many(request=request, tool_names=tool_names, k=k)
        available = ", ".join(f"{recipe.id}: {recipe.description}" for recipe in self._recipes)
        candidates = "\n\n".join(
            recipe.to_prompt(heading=f"Candidate {index}")
            for index, recipe in enumerate(selected, start=1)
        )
        return (
            "Planning cookbook:\n"
            "- The ranked candidate recipes below are selected by a best-effort "
            "heuristic; they are scaffolds, not templates. Compare nearby shapes, "
            "then adapt, combine, or ignore them when the user's request, "
            "available tools, or validator constraints require a different DAG.\n"
            "- Never copy the skeleton verbatim unless it exactly fits the request. "
            "Make the plan right-sized: include every node needed for correctness, "
            "but avoid redundant or decorative nodes.\n"
            f"- Available recipes: {available}\n\n"
            f"Top candidate recipes:\n{candidates}"
        )


def _recipe_score(
    recipe: CookbookRecipe,
    *,
    request_lower: str,
    tool_names: set[str],
) -> int:
    score = 0
    for keyword in recipe.keywords:
        if keyword.lower() in request_lower:
            score += 3
    if recipe.id == "web_research" and any(
        "web" in name or "search" in name for name in tool_names
    ):
        score += 1
    if recipe.id == "single_tool" and _request_mentions_tool_capability(
        request_lower=request_lower,
        tool_names=tool_names,
    ):
        score += 4
    if recipe.id == "direct_answer":
        score += 1
    return score


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    items = cast("Sequence[object]", value)
    return tuple(str(item) for item in items)


def _request_mentions_tool_capability(*, request_lower: str, tool_names: set[str]) -> bool:
    for name in tool_names:
        if name in request_lower:
            return True
        parts = [part for part in name.lower().split("_") if len(part) > 2]
        if any(part in request_lower for part in parts):
            return True
    return False
