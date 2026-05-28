"""Planner — turns a user request into a validated :class:`Plan`.

One LLM call in JSON mode, then a structural validation pass. If the
returned JSON parses but the plan fails validation, the orchestrator (not
the planner) decides whether to replan.
"""

from __future__ import annotations

import json
import logging

from dagagent.core import Plan
from dagagent.harness import ToolHarness
from dagagent.providers import Message, TierRouter, collect
from dagagent.validator import PlanValidator

log = logging.getLogger(__name__)

PLANNER_SYSTEM = """\
You are a task planning engine. Given a user request and a set of available tools,
produce a structured execution plan as JSON.

Rules:
- Decompose the task into sequential nodes
- Each node has exactly one clear action
- Identify points where the next action depends on what a previous node returned
- At those points, insert a DECISION node with a specific BINARY condition
- Decision branches list node IDs to execute in each case
- Decision nesting must not exceed 2 levels
- context_needed lists node IDs whose output is required — keep this minimal
- If a node needs a tool, set type=tool and tool=<name>
- If a node synthesises previous outputs, set type=synthesis
- If a node performs explicit multi-step reasoning over prior context
  (with no tool call and no final synthesis), set type=think
- If a node condenses noisy upstream output for a downstream consumer,
  set type=summary
- The final node whose output is the user's answer should be type=result;
  use synthesis for intermediate combination steps
- reasoning_required=true only for nodes needing genuine multi-step logic
  beyond what type=think already implies

Output ONLY valid JSON matching this schema (no markdown, no explanation):
{
  "nodes": [
    {
      "id": <int>,
      "type": "tool" | "decision" | "synthesis" | "think" | "summary" | "result",
      "description": "<what this node does>",
      "tool": "<tool_name or null>",
      "args_template": {<static args dict or null>},
      "context_needed": [<node ids>],
      "condition": "<binary question for decision nodes, null otherwise>",
      "branches": {"yes": [<node ids>], "no": [<node ids>]} | null,
      "default_branch": "yes" | "no" | null,
      "reasoning_required": true | false,
      "forced_tier": null
    }
  ]
}
"""


class Planner:
    """Generates plans by calling an LLM and validating the result."""

    def __init__(
        self,
        *,
        router: TierRouter,
        harness: ToolHarness,
        validator: PlanValidator,
    ) -> None:
        self._router = router
        self._harness = harness
        self._validator = validator

    async def plan(self, request: str, *, min_tier: int = 0) -> Plan:
        """Generate and validate a plan for ``request``."""
        tool_schemas_str = json.dumps(self._harness.all_schemas(), indent=2)
        messages: list[Message] = [
            {"role": "system", "content": PLANNER_SYSTEM},
            {
                "role": "user",
                "content": (f"Available tools:\n{tool_schemas_str}\n\nUser request: {request}"),
            },
        ]

        stream = self._router.complete(
            messages,
            min_tier=min_tier,
            json_mode=True,
            max_tokens=2048,
        )
        raw, tokens = await collect(stream)
        log.info("Plan generated (%d tokens)", tokens)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Planner returned invalid JSON: {exc}\nRaw: {raw[:500]}") from exc

        plan = Plan.model_validate(data)
        self._validator.validate(plan)
        return plan
