"""Eval scenarios — the fixture suite graded by ``uv run pytest -m eval``.

Each :class:`Scenario` carries the canned LLM responses that drive a
deterministic run. They are consumed in the exact order the engine makes
calls, so the script mirrors the plan-then-walk-the-DAG sequence:

    planner call
    -> for each executed node, in topological order:
         tool       : (arg-extraction call if it has context and no args) then
                      an output-shape-check call
         decision   : one branch call
         think/summary/synthesis/result : one call
         subplan    : the nested plan's calls, recursively
       (skipped branch nodes make no calls)

Keeping the scripts honest is enforced by the suite: a run that leaves
responses unconsumed (or asks for one that isn't there) fails the scenario.
"""

from __future__ import annotations

import json

from dagagent.core import NodeType, TaskStatus
from tests.eval.harness import Expectations, Scenario

# A tool whose output passes the executor's shape-check gate.
_SHAPE_OK = json.dumps({"matches_expectation": True, "issue": None})


# ── 1. Plain tool → result ─────────────────────────────────────────────────

_PLAN_CALENDAR = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "tool",
                "description": "Look up the calendar for the date",
                "tool": "calendar_get",
                "args_template": {"date": "2026-05-29"},
                "context_needed": [],
            },
            {
                "id": 2,
                "type": "result",
                "description": "Report the day's schedule to the user",
                "context_needed": [1],
            },
        ]
    }
)
_CALENDAR_RESULT = json.dumps(
    {"output": "You have nothing scheduled on 2026-05-29.", "confidence": 0.95}
)


# ── 2. think → tool (arg extraction) → summary → result ─────────────────────

_PLAN_REASONED = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "think",
                "description": "Decide which date the user is asking about",
                "context_needed": [],
            },
            {
                "id": 2,
                "type": "tool",
                "description": "Fetch the calendar for that date",
                "tool": "calendar_get",
                "context_needed": [1],
            },
            {
                "id": 3,
                "type": "summary",
                "description": "Condense the calendar result",
                "context_needed": [2],
            },
            {
                "id": 4,
                "type": "result",
                "description": "Tell the user whether they are free",
                "context_needed": [3],
            },
        ]
    }
)
_THINK_OUT = json.dumps({"reasoning": "The user means today, 2026-05-29.", "confidence": 0.8})
_EXTRACTED_ARGS = json.dumps({"date": "2026-05-29"})
_SUMMARY_OUT = json.dumps({"summary": "No events on 2026-05-29.", "confidence": 0.9})
_REASONED_RESULT = json.dumps(
    {"output": "You're completely free on 2026-05-29.", "confidence": 0.92}
)


# ── 3. tool → decision → (synthesis | result), taking the 'no' branch ───────

_PLAN_BRANCH = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "tool",
                "description": "Search the web for the AAPL price",
                "tool": "web_search",
                "args_template": {"query": "AAPL stock price today"},
                "context_needed": [],
            },
            {
                "id": 2,
                "type": "decision",
                "description": "Check whether the search returned results",
                "condition": "Did the search return any results?",
                "context_needed": [1],
                "branches": {"yes": [3], "no": [4]},
                "default_branch": "no",
            },
            {
                "id": 3,
                "type": "synthesis",
                "description": "Summarise the price data",
                "context_needed": [1],
            },
            {
                "id": 4,
                "type": "result",
                "description": "Tell the user no data was found",
                "context_needed": [1],
            },
        ]
    }
)
_DECISION_NO = json.dumps(
    {"branch": "no", "confidence": 0.88, "reasoning": "The results list was empty."}
)
_BRANCH_RESULT = json.dumps(
    {"output": "I couldn't find any AAPL price data right now.", "confidence": 0.9}
)


# ── 4. subplan → result ─────────────────────────────────────────────────────

_PLAN_SUBPLAN = json.dumps(
    {
        "nodes": [
            {
                "id": 1,
                "type": "subplan",
                "description": "Research the topic as a self-contained sub-task",
                "context_needed": [],
                "subplan": {
                    "nodes": [
                        {
                            "id": 1,
                            "type": "tool",
                            "description": "Search the web for the topic",
                            "tool": "web_search",
                            "args_template": {"query": "topic X"},
                            "context_needed": [],
                        },
                        {
                            "id": 2,
                            "type": "result",
                            "description": "Summarise the findings",
                            "context_needed": [1],
                        },
                    ]
                },
            },
            {
                "id": 2,
                "type": "result",
                "description": "Present the research to the user",
                "context_needed": [1],
            },
        ]
    }
)
_SUBPLAN_INNER_RESULT = json.dumps(
    {"output": "Findings on X: nothing notable yet.", "confidence": 0.85}
)
_SUBPLAN_RESULT = json.dumps(
    {"output": "Here is the research on X: nothing notable yet.", "confidence": 0.9}
)


SCENARIOS: list[Scenario] = [
    Scenario(
        id="tool_then_result",
        description="A single tool call feeding a result node — the happy path.",
        request="What's on my calendar for May 29th?",
        script=(_PLAN_CALENDAR, _SHAPE_OK, _CALENDAR_RESULT),
        expect=Expectations(
            min_nodes=2,
            max_nodes=2,
            node_types=(NodeType.TOOL, NodeType.RESULT),
            tools=("calendar_get",),
            final_output_contains=("nothing scheduled",),
        ),
    ),
    Scenario(
        id="reasoned_extraction",
        description="think → tool with argument extraction → summary → result.",
        request="Work out which day I mean and tell me if I'm free.",
        script=(
            _PLAN_REASONED,
            _THINK_OUT,
            _EXTRACTED_ARGS,
            _SHAPE_OK,
            _SUMMARY_OUT,
            _REASONED_RESULT,
        ),
        expect=Expectations(
            min_nodes=4,
            max_nodes=4,
            node_types=(NodeType.THINK, NodeType.TOOL, NodeType.SUMMARY, NodeType.RESULT),
            tools=("calendar_get",),
            final_output_contains=("free",),
        ),
    ),
    Scenario(
        id="decision_branch_no",
        description="A decision routes to its 'no' branch; the other branch is skipped.",
        request="Search for today's AAPL price and report it.",
        script=(_PLAN_BRANCH, _SHAPE_OK, _DECISION_NO, _BRANCH_RESULT),
        expect=Expectations(
            min_nodes=4,
            max_nodes=4,
            node_types=(NodeType.TOOL, NodeType.DECISION, NodeType.RESULT),
            tools=("web_search",),
            branches=((2, "no"),),
            final_output_contains=("couldn't find",),
        ),
    ),
    Scenario(
        id="nested_subplan",
        description="A subplan node runs a nested DAG; its output bubbles up to a result.",
        request="Research topic X using a sub-task, then summarise it for me.",
        script=(_PLAN_SUBPLAN, _SHAPE_OK, _SUBPLAN_INNER_RESULT, _SUBPLAN_RESULT),
        expect=Expectations(
            min_nodes=2,
            max_nodes=2,
            node_types=(NodeType.SUBPLAN, NodeType.RESULT),
            final_output_contains=("research on x",),
        ),
    ),
    Scenario(
        id="planning_failure",
        description="The model never returns valid JSON; planning fails after its retries.",
        request="??? garbled ???",
        script=("not valid json", "still not valid json"),
        expect=Expectations(status=TaskStatus.FAILED, must_validate=False),
        deterministic_only=True,
    ),
]
