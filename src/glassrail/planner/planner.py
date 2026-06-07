"""Planner — turns a user request into a validated :class:`Plan`.

One LLM call in JSON mode, then a structural validation pass. If the
returned JSON parses but the plan fails validation, the orchestrator (not
the planner) decides whether to replan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from glassrail.config import Settings
from glassrail.core import Plan, PlanningAttempt, PlanRejectedError, PlanValidationError
from glassrail.harness import ToolHarness
from glassrail.planner.cookbook import PlannerCookbook
from glassrail.planner.tool_digest import render_tool_capability_digest
from glassrail.providers import Message, TierRouter, collect, strip_model_output
from glassrail.telemetry import ATTR_MIN_TIER, ATTR_PLAN_NODE_COUNT, SPAN_PLAN, get_tracer
from glassrail.validator import PlanValidator

log = logging.getLogger(__name__)

_LOCAL_TIER_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


class Planner:
    """Generates plans by calling an LLM and validating the result."""

    def __init__(
        self,
        *,
        router: TierRouter,
        harness: ToolHarness,
        validator: PlanValidator,
        settings: Settings,
        cookbook: PlannerCookbook | None = None,
    ) -> None:
        self._router = router
        self._harness = harness
        self._validator = validator
        self._settings = settings
        self._cookbook = cookbook or PlannerCookbook.load_default()
        # Failed planning attempts are written here for post-mortem inspection.
        # Anchored to the user's home dir so it works regardless of which CWD
        # the process was launched from (e.g. the TUI spawns glassrail acp from
        # an arbitrary directory).
        self._failed_plan_dir = Path.home() / ".glassrail" / "failed_plans"

    async def plan(
        self, request: str, *, min_tier: int | None = None, feedback: str | None = None
    ) -> Plan:
        """Generate and validate a plan for ``request``.

        ``feedback`` (set on a guided replan after a user rejects a plan) is
        woven into the planning prompt so the next plan addresses it.

        Strategy: first attempt suppresses thinking (``/no_think``) for speed.
        If that attempt fails with a rejection or validation error — problems
        that may benefit from extended reasoning — a second attempt is made with
        thinking re-enabled. Timeout/stall/JSON failures don't improve with
        thinking and are surfaced immediately.
        """
        effective_min_tier = self._settings.planner_min_tier if min_tier is None else min_tier
        attempt = await self.plan_attempt(
            request,
            attempt=0,
            min_tier=effective_min_tier,
            feedback=feedback,
            thinking=False,
        )

        if attempt.plan is not None:
            return attempt.plan

        # Retry with thinking on errors that extended reasoning might fix.
        if attempt.error_type in ("rejection", "validation"):
            if attempt.filepath:
                log.warning(
                    "Plan attempt 0 failed (%s), retrying with thinking; written to %s",
                    attempt.error_type,
                    attempt.filepath,
                )
            else:
                log.info(
                    "Plan attempt 0 failed (%s), retrying with thinking",
                    attempt.error_type,
                )
            retry = await self.plan_attempt(
                request,
                attempt=1,
                min_tier=effective_min_tier,
                feedback=feedback,
                thinking=True,
                validation_feedback=attempt.error if attempt.error_type == "validation" else None,
            )
            if retry.filepath:
                log.warning("Retry plan failed, written to %s", retry.filepath)
            if retry.plan is not None:
                return retry.plan
            if retry.error_type == "rejection":
                raise PlanRejectedError(retry.error or "Task rejected by planner")
            if retry.error_type == "validation":
                raise PlanValidationError(retry.error or "Plan failed validation")
            raise ValueError(retry.error or "Planner failed")

        if attempt.filepath:
            log.warning("Plan failed, written to %s", attempt.filepath)
        if attempt.error_type == "rejection":
            raise PlanRejectedError(attempt.error or "Task rejected by planner")
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

    def _tier_block(self, *, min_tier: int) -> str:
        """Describe the runtime routing surface the plan will execute on.

        The planner does not route nodes at execution time, but it needs to
        know which tiers are plausibly usable so it does not casually emit
        high-reasoning nodes when only the local tier is configured.
        """
        lines = [
            "Tier routing context:",
            "- The executor chooses tiers deterministically; do not use node "
            "types as a way to pick a model.",
            "- Leave forced_tier null unless the user explicitly asks for a "
            "specific tier or a node truly cannot run on the default route.",
        ]
        for index, tier in enumerate(self._settings.tiers):
            status = self._tier_status(index=index, min_tier=min_tier)
            lines.append(f"- tier {index}: model={tier.model}, endpoint={tier.base_url}, {status}")
        lines.extend(
            [
                "- Default routing: tool, decision, summary, synthesis, and result "
                "nodes start at tier 0.",
                "- Think nodes and reasoning_required=true nodes start at tier 2. "
                "Use them only for real multi-step reasoning, and prefer ordinary "
                "summary/synthesis/result nodes when tier 2+ are not configured.",
                "- If you set forced_tier, choose only an eligible configured tier "
                "from the list above.",
            ]
        )
        return "\n".join(lines)

    def _tier_status(self, *, index: int, min_tier: int) -> str:
        tier = self._settings.tiers[index]
        if index < min_tier:
            return f"ineligible for this call (below min_tier={min_tier})"
        if tier.api_key:
            return "configured"
        hostname = urlparse(tier.base_url).hostname or ""
        if hostname in _LOCAL_TIER_HOSTS:
            return "configured local endpoint"
        return "not configured (missing API key)"

    def _failed(self, attempt: PlanningAttempt) -> PlanningAttempt:
        """Write ``attempt`` to disk and return a copy with ``filepath`` set."""
        try:
            fp = self._write_failed_attempt(attempt)
            return attempt.model_copy(update={"filepath": fp})
        except Exception:
            log.exception("Could not write failed plan attempt to disk")
            return attempt

    def _write_failed_attempt(self, attempt: PlanningAttempt) -> str:
        """Write a failed planning attempt to a JSON file and return the path."""
        filename = f"failed_plan_{attempt.attempt}_{uuid.uuid4().hex[:8]}.json"
        filepath = self._failed_plan_dir / filename
        self._failed_plan_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "attempt": attempt.attempt,
            "error_type": attempt.error_type,
            "error": attempt.error,
            "error_detail": attempt.error_detail,
            "raw_output": attempt.raw_output,
            "parsed": attempt.parsed,
            "tokens_used": attempt.tokens_used,
        }
        with filepath.open("w") as f:
            json.dump(payload, f, indent=2)
        # Return the absolute path so callers can surface it to users regardless
        # of what the process CWD was when the file was written.
        return str(filepath.resolve())

    async def plan_attempt(
        self,
        request: str,
        *,
        attempt: int,
        min_tier: int = 0,
        feedback: str | None = None,
        prior_reasoning: str | None = None,
        validation_feedback: str | None = None,
        thinking: bool = False,
    ) -> PlanningAttempt:
        """Generate one plan attempt and retain raw output plus validation errors.

        ``prior_reasoning`` carries accumulated output from a previous attempt
        that failed to emit valid JSON (a stall), so the next attempt can avoid
        repeating it.

        ``validation_feedback`` carries the validator/schema failure from the
        immediately preceding attempt, letting the model correct a concrete plan
        defect instead of retrying cold.

        ``thinking`` re-enables extended reasoning on the model by stripping the
        ``/no_think`` directive. Used on the retry attempt after a rejection or
        validation failure when additional reasoning may help.
        """
        with get_tracer().start_as_current_span(SPAN_PLAN) as span:
            span.set_attribute(ATTR_MIN_TIER, min_tier)
            tool_schemas = self._harness.all_schemas()
            tool_schemas_str = json.dumps(tool_schemas, indent=2)
            cookbook = self._cookbook.to_prompt(
                request=request,
                tool_names=self._harness.all_names(),
            )
            tool_digest = render_tool_capability_digest(tool_schemas)
            user_content = (
                f"{self._limits_block()}\n\n"
                f"{self._tier_block(min_tier=min_tier)}\n\n"
                f"{cookbook}\n\n"
                f"{tool_digest}\n\n"
                f"Available tools:\n{tool_schemas_str}\n\n"
                f"User request: {request}"
            )
            if feedback:
                user_content += (
                    "\n\nA previous plan for this request was rejected. Produce a "
                    "revised plan that addresses this feedback:\n"
                    f"{feedback}"
                )
            if prior_reasoning:
                user_content += (
                    "\n\nA previous planning attempt produced output that could not "
                    "be parsed as a valid plan. The raw output was:\n"
                    f"<previous_attempt>\n{prior_reasoning[:2000]}\n</previous_attempt>\n\n"
                    "Do not repeat this output. Emit only a valid JSON plan or a "
                    "rejection object."
                )
            if validation_feedback:
                user_content += (
                    "\n\nA previous planning attempt failed schema or structural "
                    "validation. Produce a corrected plan that fixes this exact "
                    "problem while preserving the user's intent:\n"
                    f"<validation_feedback>\n{validation_feedback}\n</validation_feedback>"
                )
            system_prompt = self._settings.prompts.planner
            if thinking:
                # Re-enable extended reasoning by stripping the /no_think directive
                # that the default planner prompt appends. Used on retry attempts
                # where initial fast planning rejected or failed validation.
                system_prompt = system_prompt.removesuffix("\n/no_think")

            messages: list[Message] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            stream = self._router.complete(
                messages,
                min_tier=min_tier,
                json_mode=True,
                max_tokens=self._settings.budgets.planner,
            )
            timeout = (
                self._settings.planner_retry_timeout_s
                if thinking
                else self._settings.planner_initial_timeout_s
            )
            try:
                raw, tokens = await asyncio.wait_for(collect(stream), timeout=float(timeout))
            except TimeoutError:
                return self._failed(
                    PlanningAttempt(
                        attempt=attempt,
                        raw_output="",
                        error=f"Planner timed out after {timeout}s",
                        error_type="timeout",
                        tokens_used=0,
                    )
                )
            log.info("Plan generated (%d tokens)", tokens)

            cleaned = strip_model_output(raw)
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                is_stall = len(raw) > self._planner_stall_char_limit()
                return self._failed(
                    PlanningAttempt(
                        attempt=attempt,
                        raw_output=raw,
                        error=f"Planner returned invalid JSON: {exc}",
                        error_type="stall" if is_stall else "json",
                        error_detail=raw if is_stall else None,
                        tokens_used=tokens,
                    )
                )

            if not isinstance(data, dict):
                return self._failed(
                    PlanningAttempt(
                        attempt=attempt,
                        raw_output=raw,
                        error=f"Planner returned {type(data).__name__}, expected JSON object",
                        error_type="schema",
                        tokens_used=tokens,
                    )
                )
            parsed = cast("dict[str, Any]", data)

            # Rejection is checked before the plan schema so a response that
            # contains both "rejection" and "nodes" is treated as a rejection.
            if "rejection" in parsed:
                reason = str(parsed["rejection"])
                rejection_class = self._classify_rejection(request, reason)
                log.warning(
                    "Planner rejected task",
                    extra={
                        "rejection_reason": reason,
                        "rejection_class": rejection_class,
                    },
                )
                return self._failed(
                    PlanningAttempt(
                        attempt=attempt,
                        raw_output=raw,
                        parsed=parsed,
                        error=reason,
                        error_type="rejection",
                        tokens_used=tokens,
                    )
                )

            try:
                plan = Plan.model_validate(parsed)
                self._validator.validate(plan)
            except (PlanValidationError, ValueError) as exc:
                etype = "validation" if isinstance(exc, PlanValidationError) else "schema"
                return self._failed(
                    PlanningAttempt(
                        attempt=attempt,
                        raw_output=raw,
                        parsed=parsed,
                        error=str(exc),
                        error_type=etype,
                        tokens_used=tokens,
                    )
                )
            span.set_attribute(ATTR_PLAN_NODE_COUNT, len(plan.nodes))
            return PlanningAttempt(
                attempt=attempt,
                raw_output=raw,
                parsed=parsed,
                plan=plan,
                tokens_used=tokens,
            )

    def _planner_stall_char_limit(self) -> int:
        """Character threshold for classifying invalid planner output as a stall."""
        return self._settings.budgets.planner * self._settings.planner_stall_char_multiplier

    @staticmethod
    def _classify_rejection(request: str, reason: str) -> str:
        """Best-effort label for operator logs; does not change behaviour."""
        text = f"{request} {reason}".lower()
        suspected_keywords = (
            "predict",
            "prediction",
            "forecast",
            "recommend",
            "recommendation",
            "judge",
            "opinion",
            "factual",
            "general knowledge",
            "clarifying",
            "vague",
            "unknown",
            "unknowable",
        )
        return (
            "suspected_mistaken"
            if any(keyword in text for keyword in suspected_keywords)
            else "legitimate"
        )
