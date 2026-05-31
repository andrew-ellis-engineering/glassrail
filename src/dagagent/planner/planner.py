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

    async def plan(self, request: str, *, min_tier: int = 0, feedback: str | None = None) -> Plan:
        """Generate and validate a plan for ``request``.

        ``feedback`` (set on a guided replan after a user rejects a plan) is
        woven into the planning prompt so the next plan addresses it.
        """
        attempt = await self.plan_attempt(request, attempt=0, min_tier=min_tier, feedback=feedback)
        if attempt.plan is not None:
            return attempt.plan
        if attempt.error_type == "validation":
            raise PlanValidationError(attempt.error or "Plan failed validation")
        raise ValueError(attempt.error or "Planner failed")

    def _limits_block(self) -> str:
        """The structural budget the validator enforces, stated for the model.

        Injected per-request (not baked into the system prompt) so it tracks
        settings and survives a user-overridden ``prompts.planner``. Without
        this the model never learns the node cap and happily overshoots it.
        """
        return (
            "Plan limits (a plan exceeding these is rejected):\n"
            f"- At most {self._settings.max_plan_nodes} nodes in this plan.\n"
            f"- At most {self._settings.max_subplans_per_plan} subplan node(s), "
            f"each with at most {self._settings.max_subplan_nodes} nodes.\n"
            "Stay within the node budget: if the task is large, consolidate "
            "related steps into one node rather than exceeding the limit."
        )

    async def plan_attempt(
        self,
        request: str,
        *,
        attempt: int,
        min_tier: int = 0,
        feedback: str | None = None,
    ) -> PlanningAttempt:
        """Generate one plan attempt and retain raw output plus validation errors."""
        with get_tracer().start_as_current_span(SPAN_PLAN) as span:
            span.set_attribute(ATTR_MIN_TIER, min_tier)
            tool_schemas_str = json.dumps(self._harness.all_schemas(), indent=2)
            user_content = (
                f"{self._limits_block()}\n"
                f"Available tools:\n{tool_schemas_str}\n\n"
                f"User request: {request}"
            )
            if feedback:
                user_content += (
                    "\n\nA previous plan for this request was rejected. Produce a "
                    "revised plan that addresses this feedback:\n"
                    f"{feedback}"
                )
            messages: list[Message] = [
                {"role": "system", "content": self._settings.prompts.planner},
                {"role": "user", "content": user_content},
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
