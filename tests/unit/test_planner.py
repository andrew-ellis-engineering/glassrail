"""Tests for the Planner."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from dagagent.config import NodeBudgets, NodePrompts, Settings
from dagagent.core import NodeType, PlanValidationError
from dagagent.harness import ToolHarness, register_builtins
from dagagent.planner import Planner
from dagagent.providers import Chunk, Message, TierRouter
from dagagent.validator import PlanValidator


class _FixedProvider:
    """Fake provider that emits a pre-set payload as a single chunk."""

    def __init__(self, *, payload: str, name: str = "fake", tier: int = 0) -> None:
        self._payload = payload
        self._name = name
        self._tier = tier

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> int:
        return self._tier

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        del messages, json_mode, max_tokens, timeout_s
        yield Chunk(text=self._payload, tokens_used=42)


@pytest.fixture
def harness() -> ToolHarness:
    h = ToolHarness()
    register_builtins(h)
    return h


@pytest.fixture
def settings() -> Settings:
    return Settings()


def _planner_from(provider: _FixedProvider, harness: ToolHarness, settings: Settings) -> Planner:
    router = TierRouter([provider])
    validator = PlanValidator(harness=harness, settings=settings)
    return Planner(router=router, harness=harness, validator=validator, settings=settings)


async def test_plan_round_trips_simple_payload(harness: ToolHarness, settings: Settings) -> None:
    payload = json.dumps(
        {
            "nodes": [
                {
                    "id": 1,
                    "type": "tool",
                    "description": "get today's events",
                    "tool": "calendar_get",
                    "args_template": {"date": "2026-05-27"},
                    "context_needed": [],
                },
            ],
        }
    )
    planner = _planner_from(_FixedProvider(payload=payload), harness, settings)
    plan = await planner.plan("what do I have today?")
    assert len(plan.nodes) == 1
    assert plan.nodes[0].type is NodeType.TOOL
    assert plan.nodes[0].tool == "calendar_get"
    assert plan.sorted_node_ids == [1]


class _CapturingProvider(_FixedProvider):
    """Records the ``max_tokens`` and messages of each call."""

    def __init__(self, *, payload: str) -> None:
        super().__init__(payload=payload)
        self.max_tokens_seen: list[int] = []
        self.system_seen: list[str] = []
        self.user_seen: list[str] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        self.max_tokens_seen.append(max_tokens)
        self.system_seen.append(next(m["content"] for m in messages if m["role"] == "system"))
        self.user_seen.append(next(m["content"] for m in messages if m["role"] == "user"))
        del json_mode, timeout_s
        yield Chunk(text=self._payload, tokens_used=42)


async def test_planner_uses_its_configured_budget(harness: ToolHarness) -> None:
    """The plan generation call is capped at the configured planner budget."""
    payload = json.dumps({"nodes": [{"id": 1, "type": "result", "description": "x"}]})
    provider = _CapturingProvider(payload=payload)
    settings = Settings(budgets=NodeBudgets(planner=5005))
    planner = _planner_from(provider, harness, settings)

    await planner.plan("anything")
    assert provider.max_tokens_seen == [5005]


async def test_planner_uses_its_configured_prompt(harness: ToolHarness) -> None:
    """A custom planner prompt is sent as the system message."""
    payload = json.dumps({"nodes": [{"id": 1, "type": "result", "description": "x"}]})
    provider = _CapturingProvider(payload=payload)
    settings = Settings(prompts=NodePrompts(planner="CUSTOM PLANNER PROMPT"))
    planner = _planner_from(provider, harness, settings)

    await planner.plan("anything")
    assert provider.system_seen == ["CUSTOM PLANNER PROMPT"]


async def test_planner_tells_model_the_node_limits(harness: ToolHarness) -> None:
    """The configured plan/subplan caps are injected into the request so the
    model knows its budget — even when the system prompt is overridden."""
    payload = json.dumps({"nodes": [{"id": 1, "type": "result", "description": "x"}]})
    provider = _CapturingProvider(payload=payload)
    settings = Settings(
        max_plan_nodes=24,
        max_subplans_per_plan=2,
        max_subplan_nodes=12,
        prompts=NodePrompts(planner="CUSTOM"),
    )
    planner = _planner_from(provider, harness, settings)

    await planner.plan("anything")
    user_msg = provider.user_seen[0]
    assert "At most 24 nodes" in user_msg
    assert "At most 2 subplan node(s)" in user_msg
    assert "at most 12 nodes" in user_msg


async def test_feedback_is_woven_into_the_planning_prompt(
    harness: ToolHarness, settings: Settings
) -> None:
    """On a guided replan, the user's feedback is injected into the request."""
    payload = json.dumps({"nodes": [{"id": 1, "type": "result", "description": "x"}]})
    provider = _CapturingProvider(payload=payload)
    planner = _planner_from(provider, harness, settings)

    await planner.plan("summarise the doc", feedback="use bullet points, not prose")
    user_msg = provider.user_seen[0]
    assert "previous plan" in user_msg.lower()
    assert "use bullet points, not prose" in user_msg


async def test_no_feedback_leaves_prompt_clean(harness: ToolHarness, settings: Settings) -> None:
    """Without feedback the revision block is absent from the prompt."""
    payload = json.dumps({"nodes": [{"id": 1, "type": "result", "description": "x"}]})
    provider = _CapturingProvider(payload=payload)
    planner = _planner_from(provider, harness, settings)

    await planner.plan("summarise the doc")
    assert "previous plan was rejected" not in provider.user_seen[0].lower()


async def test_invalid_json_raises_value_error(harness: ToolHarness, settings: Settings) -> None:
    planner = _planner_from(_FixedProvider(payload="not json at all"), harness, settings)
    with pytest.raises(ValueError, match="invalid JSON"):
        await planner.plan("hi")


async def test_plan_attempt_captures_invalid_json(harness: ToolHarness, settings: Settings) -> None:
    planner = _planner_from(_FixedProvider(payload="not json at all"), harness, settings)
    attempt = await planner.plan_attempt("hi", attempt=2)
    assert attempt.attempt == 2
    assert attempt.raw_output == "not json at all"
    assert attempt.error_type == "json"
    assert attempt.error is not None
    assert attempt.plan is None
    assert attempt.valid is False


async def test_plan_validation_errors_propagate(harness: ToolHarness, settings: Settings) -> None:
    payload = json.dumps(
        {
            "nodes": [
                {
                    "id": 1,
                    "type": "tool",
                    "description": "use a tool that doesn't exist",
                    "tool": "totally_bogus",
                    "context_needed": [],
                }
            ]
        }
    )
    planner = _planner_from(_FixedProvider(payload=payload), harness, settings)
    with pytest.raises(PlanValidationError, match="unknown tools"):
        await planner.plan("hi")


async def test_plan_attempt_captures_validation_error(
    harness: ToolHarness, settings: Settings
) -> None:
    payload = json.dumps(
        {
            "nodes": [
                {
                    "id": 1,
                    "type": "tool",
                    "description": "use a tool that doesn't exist",
                    "tool": "totally_bogus",
                    "context_needed": [],
                }
            ]
        }
    )
    planner = _planner_from(_FixedProvider(payload=payload), harness, settings)
    attempt = await planner.plan_attempt("hi", attempt=0)
    assert attempt.parsed is not None
    assert attempt.error_type == "validation"
    assert attempt.error is not None
    assert "unknown tools" in attempt.error
