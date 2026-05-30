"""Planner — turns a user request into a validated :class:`Plan`.

One LLM call in JSON mode, then a structural validation pass. If the
returned JSON parses but the plan fails validation, the orchestrator (not
the planner) decides whether to replan.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from dagagent.config import Settings
from dagagent.core import Plan, PlanningAttempt, PlanValidationError
from dagagent.harness import ToolHarness
from dagagent.providers import Message, TierRouter, collect
from dagagent.telemetry import ATTR_MIN_TIER, ATTR_PLAN_NODE_COUNT, SPAN_PLAN, get_tracer
from dagagent.validator import PlanValidator

log = logging.getLogger(__name__)


class Planner:
    """Generates plans by calling an LLM and validating the result."""

    def __init__(
        self,
        *,
        router: TierRouter,
        harness: ToolHarness,
        validator: PlanValidator,
        settings: Settings,
    ) -> None:
        self._router = router
        self._harness = harness
        self._validator = validator
        self._settings = settings

    async def plan(self, request: str, *, min_tier: int = 0) -> Plan:
        """Generate and validate a plan for ``request``."""
        attempt = await self.plan_attempt(request, attempt=0, min_tier=min_tier)
        if attempt.plan is not None:
            return attempt.plan
        if attempt.error_type == "validation":
            raise PlanValidationError(attempt.error or "Plan failed validation")
        raise ValueError(attempt.error or "Planner failed")

    async def plan_attempt(
        self,
        request: str,
        *,
        attempt: int,
        min_tier: int = 0,
    ) -> PlanningAttempt:
        """Generate one plan attempt and retain raw output plus validation errors."""
        with get_tracer().start_as_current_span(SPAN_PLAN) as span:
            span.set_attribute(ATTR_MIN_TIER, min_tier)
            tool_schemas_str = json.dumps(self._harness.all_schemas(), indent=2)
            messages: list[Message] = [
                {"role": "system", "content": self._settings.prompts.planner},
                {
                    "role": "user",
                    "content": (f"Available tools:\n{tool_schemas_str}\n\nUser request: {request}"),
                },
            ]

            stream = self._router.complete(
                messages,
                min_tier=min_tier,
                json_mode=True,
                max_tokens=self._settings.budgets.planner,
            )
            raw, tokens = await collect(stream)
            log.info("Plan generated (%d tokens)", tokens)

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                return PlanningAttempt(
                    attempt=attempt,
                    raw_output=raw,
                    error=f"Planner returned invalid JSON: {exc}",
                    error_type="json",
                    tokens_used=tokens,
                )

            if not isinstance(data, dict):
                return PlanningAttempt(
                    attempt=attempt,
                    raw_output=raw,
                    error=f"Planner returned {type(data).__name__}, expected JSON object",
                    error_type="schema",
                    tokens_used=tokens,
                )
            parsed = cast("dict[str, Any]", data)

            try:
                plan = Plan.model_validate(parsed)
                self._validator.validate(plan)
            except PlanValidationError as exc:
                return PlanningAttempt(
                    attempt=attempt,
                    raw_output=raw,
                    parsed=parsed,
                    error=str(exc),
                    error_type="validation",
                    tokens_used=tokens,
                )
            except ValueError as exc:
                return PlanningAttempt(
                    attempt=attempt,
                    raw_output=raw,
                    parsed=parsed,
                    error=str(exc),
                    error_type="schema",
                    tokens_used=tokens,
                )
            span.set_attribute(ATTR_PLAN_NODE_COUNT, len(plan.nodes))
            return PlanningAttempt(
                attempt=attempt,
                raw_output=raw,
                parsed=parsed,
                plan=plan,
                tokens_used=tokens,
            )
