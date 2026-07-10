"""Integration test: dependent-node descriptions appear in upstream context.

Verifies that when a synthesis node is followed by a result node, the synthesis
node's LLM prompt includes the result node's description under the
"Your output will be consumed by:" header.
"""

from __future__ import annotations

import json

from glassrail.config import Settings
from glassrail.core import ExecutionState, Plan, new_task_id
from glassrail.executor import Executor
from glassrail.harness import ToolHarness, register_builtins
from glassrail.providers import TierRouter
from glassrail.validator import PlanValidator
from tests.conftest import make_capturing_scripted


async def test_synthesis_prompt_includes_downstream_description() -> None:
    """Synthesis node's prompt must mention the result node it feeds."""
    settings = Settings()
    harness = ToolHarness()
    register_builtins(harness)

    synthesis_response = json.dumps({"output": "combined finding", "confidence": 0.9})
    result_response = json.dumps({"output": "final answer", "confidence": 1.0})
    provider = make_capturing_scripted([synthesis_response, result_response])
    router = TierRouter([provider])
    executor = Executor(router=router, harness=harness, settings=settings)

    plan_data = {
        "nodes": [
            {
                "id": 1,
                "type": "synthesis",
                "description": "Combine research results into a recommendation",
                "context_needed": [],
            },
            {
                "id": 2,
                "type": "result",
                "description": "Deliver the final answer to the user",
                "context_needed": [1],
            },
        ]
    }
    plan = Plan.model_validate(plan_data)
    validator = PlanValidator(harness=harness, settings=settings)
    plan.sorted_node_ids = validator.validate(plan)

    state = ExecutionState(task_id=new_task_id(), user_request="Summarise findings")
    state.plan = plan
    await executor.execute(state)

    # The first LLM call is the synthesis node's prompt.
    assert provider.user_messages, "no LLM calls recorded"
    synthesis_prompt = provider.user_messages[0]
    assert "Your output will be consumed by:" in synthesis_prompt
    assert "Deliver the final answer to the user" in synthesis_prompt
