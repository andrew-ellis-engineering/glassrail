"""Tests for the Planner."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from dagagent.config import Settings
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
    return Planner(router=router, harness=harness, validator=validator)


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


async def test_invalid_json_raises_value_error(harness: ToolHarness, settings: Settings) -> None:
    planner = _planner_from(_FixedProvider(payload="not json at all"), harness, settings)
    with pytest.raises(ValueError, match="invalid JSON"):
        await planner.plan("hi")


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
