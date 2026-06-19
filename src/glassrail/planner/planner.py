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
                rejection_feedback=rejection_retry_feedback(attempt.error)
                if attempt.error_type == "rejection"
                else None,
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
        routing = self._settings.routing
        lines.extend(
            [
                "- Node routing table: "
                f"tool={routing.tool}, decision={routing.decision}, "
                f"summary={routing.summary}, synthesis={routing.synthesis}, "
                f"think={routing.think}, result={routing.result}.",
                "- Nodes with reasoning_required=true start at least at "
                f"tier {routing.reasoning_required}. "
                "Use them only for real multi-step reasoning, and prefer ordinary "
                "summary/synthesis/result nodes when higher tiers are not configured.",
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
        rejection_feedback: str | None = None,
        thinking: bool = False,
    ) -> PlanningAttempt:
        """Generate one plan attempt and retain raw output plus validation errors.

        ``prior_reasoning`` carries accumulated output from a previous attempt
        that failed to emit valid JSON (a stall), so the next attempt can avoid
        repeating it.

        ``validation_feedback`` carries the validator/schema failure from the
        immediately preceding attempt, letting the model correct a concrete plan
        defect instead of retrying cold.

        ``rejection_feedback`` carries guidance after a previous mistaken
        rejection of a request that can be answered with a result node.

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
            if rejection_feedback:
                user_content += (
                    "\n\nA previous planning attempt rejected an answerable request. "
                    "Produce a valid plan, not a rejection, that preserves the "
                    "user's intent and answers through an ordinary result node:\n"
                    f"<rejection_feedback>\n{rejection_feedback}\n</rejection_feedback>"
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
                data = json.loads(
                    cleaned,
                    object_pairs_hook=_prefer_non_null_duplicate_object,
                )
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

            _repair_plan_payload(parsed)
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


def rejection_retry_feedback(reason: str | None) -> str | None:
    """Return retry guidance when a rejection describes an answerable task."""
    if not reason:
        return None
    text = reason.lower()
    if _looks_like_missing_capability_rejection(text):
        return None
    if any(
        marker in text
        for marker in (
            "too vague",
            "vague",
            "underspecified",
            "clarifying",
            "please specify",
            "safe next steps",
        )
    ):
        return (
            "The request is vague or underspecified, but that is answerable. "
            "Emit a result node whose description asks one focused clarifying "
            "question or offers safe next steps. Do not emit a rejection."
        )
    if any(
        marker in text
        for marker in (
            "unknown",
            "unknowable",
            "future",
            "private",
            "random",
            "cannot be predicted",
            "cannot predict",
            "uncertainty",
            "calibrated",
            "do not fabricate",
            "not knowable",
            "unverifiable",
        )
    ):
        return (
            "The exact value may be unknown or unknowable, but that is "
            "answerable. Emit a result node whose description says the exact "
            "value is unknown or unknowable, explains why, and avoids "
            "fabrication. Do not emit a rejection."
        )
    return None


def _looks_like_missing_capability_rejection(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "send_email",
            "registered tool",
            "tool is not registered",
            "not registered",
            "required tool",
            "no suitable tools",
            "available tools",
        )
    ) and not any(
        answerable_marker in text
        for answerable_marker in (
            "calibrated uncertainty",
            "clarifying",
            "too vague",
            "vague",
            "unknown",
            "unknowable",
        )
    )


def _repair_plan_payload(plan_payload: dict[str, Any]) -> None:
    """Repair small, deterministic planner omissions before strict validation."""
    _repair_plan_descriptions(plan_payload)
    _ensure_terminal_result(plan_payload)


def _prefer_non_null_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Decode JSON objects while ignoring later duplicate nulls.

    Local models sometimes emit a real field and then repeat the same key with
    ``null`` while filling optional schema slots. Standard ``json.loads`` keeps
    the later null, turning an otherwise valid plan into a validation failure.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result and value is None and result[key] is not None:
            continue
        result[key] = value
    return result


def _repair_plan_descriptions(plan_payload: dict[str, Any]) -> None:
    """Fill repairable missing descriptions before strict plan validation."""
    nodes = plan_payload.get("nodes")
    if isinstance(nodes, list):
        _repair_node_descriptions(cast("list[Any]", nodes))


def _repair_node_descriptions(nodes: list[Any]) -> None:
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        node = cast("dict[str, Any]", raw_node)
        description = node.get("description")
        if not isinstance(description, str) or not description.strip():
            node["description"] = _fallback_node_description(node)

        raw_subplan = node.get("subplan")
        if isinstance(raw_subplan, dict):
            subplan = cast("dict[str, Any]", raw_subplan)
            _repair_plan_descriptions(subplan)


def _fallback_node_description(node: dict[str, Any]) -> str:
    node_type = str(node.get("type") or "node")
    node_id = node.get("id")
    suffix = f" {node_id}" if isinstance(node_id, int) else ""

    if node_type == "decision":
        condition = node.get("condition")
        if isinstance(condition, str) and condition.strip():
            return f"Decide whether: {condition.strip()}"
        fallback = "Choose the correct yes/no branch"
    elif node_type == "tool":
        tool = node.get("tool")
        if isinstance(tool, str) and tool.strip():
            return f"Run the {tool.strip()} tool"
        fallback = f"Execute node{suffix}"
    else:
        fallback_by_type = {
            "subplan": f"Execute subplan{suffix}",
            "summary": f"Summarize upstream context{suffix}",
            "synthesis": f"Synthesize upstream context{suffix}",
            "think": f"Reason through node{suffix}",
            "result": f"Produce the final answer{suffix}",
        }
        fallback = fallback_by_type.get(node_type, f"Execute node{suffix}")

    return fallback.strip()


def _ensure_terminal_result(plan_payload: dict[str, Any]) -> None:
    nodes = plan_payload.get("nodes")
    if not isinstance(nodes, list):
        return
    node_list = cast("list[Any]", nodes)

    for raw_node in node_list:
        if not isinstance(raw_node, dict):
            continue
        node = cast("dict[str, Any]", raw_node)
        subplan = node.get("subplan")
        if isinstance(subplan, dict):
            _ensure_terminal_result(cast("dict[str, Any]", subplan))

    typed_nodes = [cast("dict[str, Any]", node) for node in node_list if isinstance(node, dict)]
    if any(node.get("type") == "result" for node in typed_nodes):
        return

    synthesis_sink = _single_terminal_synthesis(typed_nodes)
    if synthesis_sink is None:
        return

    existing_ids: list[int] = []
    for node in typed_nodes:
        node_id = node.get("id")
        if isinstance(node_id, int):
            existing_ids.append(node_id)
    next_id = (max(existing_ids) + 1) if existing_ids else 1
    node_list.append(
        {
            "id": next_id,
            "type": "result",
            "description": "Return the synthesized answer as the final user-facing response",
            "context_needed": [synthesis_sink],
        }
    )


def _single_terminal_synthesis(nodes: list[dict[str, Any]]) -> int | None:
    referenced: set[int] = set()
    for node in nodes:
        for dep in node.get("context_needed") or []:
            if isinstance(dep, int):
                referenced.add(dep)
        branches = node.get("branches")
        if isinstance(branches, dict):
            for branch_nodes in branches.values():
                if not isinstance(branch_nodes, list):
                    continue
                referenced.update(nid for nid in branch_nodes if isinstance(nid, int))

    sinks = [
        node["id"]
        for node in nodes
        if node.get("type") == "synthesis"
        and isinstance(node.get("id"), int)
        and node["id"] not in referenced
    ]
    if len(sinks) == 1:
        return cast("int", sinks[0])
    return None
