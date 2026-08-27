# Spec: Foreach Node (Iteration in Plans)

Status: Needs further specification before implementation.
Priority: Phase 2 capability layer, after registry output schemas.
Depends on: parallel execution and upstream context awareness (implemented),
plus [Node contracts and context flow](node-contracts.md) Part 2.

---

## Problem

The planner can only express "run this operation over a list of N items" by emitting N hand-copied nodes. This requires knowing N at plan time, blows up the plan size and planning-token cost, and makes fan-out/fan-in patterns (summarize 8 articles, check 5 repos, process N files) awkward to express. It also prevents fan-out over a list that is only known at runtime (e.g., the output of a tool call).

The right fix is **not cycles in the DAG**. See locked decisions below.

---

## Locked decisions (do not relitigate)

1. **Plans are permanently acyclic.** The DAG acyclicity invariant is load-bearing — it keeps the validator, topological executor, ACP plan mapping, and OTel span tree all tractable. Cycles would require a fundamentally different scheduling model and break every property built on topological sort. This door is closed forever. Recorded in `CLAUDE.md`.

2. **Iteration is expressed within a node, never as a graph cycle.** A `foreach` node fans out over a list using the existing subplan mechanism. The loop body lives *inside* a node; the graph topology stays acyclic.

3. **Conditional loops belong at the orchestrator/session layer, not the plan layer.** "Repeat until condition X" is an `Orchestrator.revise`-style re-plan loop — one session running multiple plans. It is not a plan-level construct. The chat loop idea is the same thing: a session alternating turns, not a cyclic plan. Do not add conditional-loop primitives to the plan grammar.

4. **The initial ship has one aggregation policy: `collect`.** The node emits the ordered list of per-item outputs as structured data; a downstream node can consume it through `context_needed`. Map-to-synthesis is expressed as `foreach` with `collect` followed by an explicit `synthesis` node, preserving the plan as the complete execution document. Do not ship `concat`, `reduce`, or an implicit synthesis step in the first version.

---

## The `foreach` node type

Add `FOREACH = "foreach"` to `NodeType`.

### New fields on `Node`

```python
# FOREACH-only
foreach_source: int | list[Any] | None = None
# Either an upstream node id whose output is a list,
# or a literal list of items provided inline.

foreach_body: Plan | None = None
# The nested plan to run once per item.
# Each iteration receives the item injected as a special context key.

foreach_aggregation: Literal["collect"] = "collect"
# The node output is list[item_result], in source order.
```

### Execution model

1. When the executor reaches a `foreach` node, it resolves the source list:
   - If `foreach_source` is an `int`, look up that node's output in the current execution state. If the output is not a list, fail the node.
   - If `foreach_source` is a literal list, use it directly.
2. For each item in the list, run `foreach_body` as a nested plan (same mechanism as `subplan`). Inject the item as a special input key available to all nodes in the body via their context.
3. Iterations are **independent and parallelisable** — run with a bounded concurrency semaphore (configurable, defaults to e.g. 4). This is the natural parallelism boundary.
4. Collect results in source order and emit the list as the node output.

### Implications for event stream / OTel

The current assumption of "one node = one NodeStarted/NodeFinished event pair" must be relaxed for `foreach`. Each iteration should emit its own span as a child of the `foreach` node's span. The state store must be able to record N sub-results keyed by `(node_id, iteration_index)`. **Do not bake one-execution-per-node into these surfaces before `foreach` lands.**

---

## Validator changes

Add a node-type contract for `foreach`:
- Must have `foreach_source` set (int or non-empty list).
- Must have `foreach_body` set and the body must itself pass full plan validation (same recursive check as `subplan`).
- If `foreach_source` is an int, that node id must appear in `context_needed`.
- `foreach_aggregation` must be `"collect"`; reject any other value rather than silently accepting a future policy.
- `foreach_body` is acyclic (validated recursively — same as subplan today).
- No new cycle check needed: `foreach` is one node in the parent graph topology.

---

## Planner guidance

The planner must learn when to emit `foreach` vs. unrolled copies:
- Use `foreach` when iterating over a runtime-produced list (a previous node's output).
- Use `foreach` when N is unknown at plan time or large.
- Use unrolled copies only for tiny, known-at-plan-time N (≤ 3).
- The `foreach_body` should be a `subplan` containing the per-item work. The body should be self-contained — it receives one item and should produce one result.

Add a `foreach` recipe to the planner cookbook with examples of: summarize-each, extract-each, check-each-repo.

### The list-source prerequisite

For `foreach_source` to reference a runtime-produced list (an upstream tool's
output), the planner and validator need the producer's output schema. Upstream
context awareness is already implemented; registry output schemas are the
remaining structural prerequisite. The first focused `foreach` PR may support
literal lists only if runtime-produced source validation is split into a later,
explicitly scoped PR.

---

## Files to touch

- `src/glassrail/core/plan.py` — add `FOREACH` to `NodeType`, add `foreach_*` fields to `Node`
- `src/glassrail/validator/` — add foreach node-type contract
- `src/glassrail/executor/executor.py` — add `_execute_foreach`, bounded concurrency
- `src/glassrail/core/execution.py` — extend `NodeResult` to hold per-iteration sub-results
- `src/glassrail/events/` — extend `NodeStarted`/`NodeFinished` or add iteration-level events
- `src/glassrail/planner/` — add `foreach` cookbook recipe, planner prompt guidance
- `eval-framework/suites/glassrail/tasks/` — add `foreach` capability eval tasks
- `CLAUDE.md` — acyclicity invariant already added (see locked decisions)

---

## What this is NOT

- Not cycles in the plan grammar.
- Not a "chat loop" feature — the chat loop is a session-layer concern (Orchestrator/ACP), not a plan-level construct.
- Not `reduce` semantics or sequential fold — those are out of scope for v1.
- Not an implicit synthesis step — use an explicit downstream `synthesis` node after `collect`.
- Not conditional loops ("retry until success") — those belong at the orchestrator layer.
