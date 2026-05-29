# Evals

The eval suite scores the planner and executor against a set of fixtures. It
answers a different question from the unit and integration tests: not "does
this function behave?" but "given a request, does the agent plan and execute
it well?"

## Running

```bash
uv run pytest -m eval
```

The suite is excluded from the default `uv run pytest` sweep — it's its own
gate. Each scenario is one parametrised test (so you get ordinary pass/fail),
and the run prints an aggregate score table:

```
========================================================================
  EVAL SUMMARY (deterministic) — 5 scenarios
========================================================================
  scenario                        plan  exec  score   result
  ------------------------------------------------------------------
  tool_then_result                1.00  1.00   1.00   PASS
  reasoned_extraction             1.00  1.00   1.00   PASS
  decision_branch_no              1.00  1.00   1.00   PASS
  nested_subplan                  1.00  1.00   1.00   PASS
  planning_failure                  --  1.00   1.00   PASS
  ------------------------------------------------------------------
  5/5 passed   mean score 1.00
========================================================================
```

Scores split into a **planning** dimension (was a sensible, valid plan
produced?) and an **execution** dimension (did running it reach the right
outcome?).

## Anatomy of a scenario

A `Scenario` (see `tests/eval/scenarios.py`) pairs a request with the canned
LLM responses that drive a deterministic run and the `Expectations` it's
graded against:

```python
Scenario(
    id="tool_then_result",
    request="What's on my calendar for May 29th?",
    script=(_PLAN_CALENDAR, _SHAPE_OK, _CALENDAR_RESULT),
    expect=Expectations(
        min_nodes=2,
        max_nodes=2,
        node_types=(NodeType.TOOL, NodeType.RESULT),
        tools=("calendar_get",),
        final_output_contains=("nothing scheduled",),
    ),
)
```

`Expectations` is declarative: **only the fields you set are scored**. Each
set field becomes one weighted check, and the scenario's score is the fraction
of its checks that pass. A scenario passes when its score meets
`pass_threshold` (default `1.0`).

## Writing the script

The `script` is the list of LLM responses, consumed in the exact order the
engine makes calls:

```
planner call
-> for each executed node, in topological order:
     tool       : (arg-extraction call if it has context and no args) then
                  an output-shape-check call
     decision   : one branch call
     think / summary / synthesis / result : one call
     subplan    : the nested plan's calls, recursively
   (skipped branch nodes make no calls)
```

You don't have to count perfectly by hand: the suite fails a scenario whose
script leaves responses unused (or asks for one that isn't there), so a script
that's out of sync with the engine is caught immediately.

## Live mode

The scorer is provider-agnostic. Setting `DAGAGENT_EVAL_LIVE=1` runs the same
scenarios against the configured tier providers instead of the canned script:

```bash
DAGAGENT_EVAL_LIVE=1 uv run pytest -m eval
```

Live runs grade structure only (plan validity, node count, node types, tools)
— the exact output wording and branch taken are dropped, since a real model
phrases things its own way. Live results are **reported, not gated**: model
output varies, so the live job never blocks a PR. Tightening live scores into
a release gate is the work that unlocks the first PyPI publish (see the
[roadmap](roadmap.md)).

## In CI

CI runs `pytest -m eval` in a dedicated, non-blocking job. The deterministic
scenarios run offline and reliably; the live scenarios are skipped unless
`DAGAGENT_EVAL_LIVE=1` is set, so external-provider flakiness can never block a
pull request.
