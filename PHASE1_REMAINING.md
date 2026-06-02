# Phase 1 — Remaining Work

Five items stand between the current state and the Phase 1 exit gate
(promoting capability evals to regression, unlocking PyPI publish).
Four are buildable now; one (per-tool HITL) needs a design decision first.

---

## 1. Planner subplan guidance

**Type:** Prompt improvement — no code change.

**Problem.** The planner knows *when not* to emit a subplan (the current
prompt says don't wrap a single tool call) but has no guidance on *when to
emit one* or *what makes a good subplan boundary*. In practice the planner
rarely emits subplans, even for tasks that genuinely benefit from a nested
graph (multi-step research with independent subtopics, conditional branches
that each need several nodes).

**Change.** Extend `DEFAULT_PLANNER_SYSTEM` in `src/dagagent/config/prompts.py`
with an explicit subplan section covering:

- **When to use:** a sub-task is genuinely self-contained — it has its own
  inputs, produces one output the parent consumes, and benefits from being
  treated as a unit (e.g. "research Option A end to end" inside a
  compare-three plan).
- **When not to use:** wrapping a single node (always wrong); nesting when
  a flat fan-out with a synthesis node would be clearer; nesting deeper than
  one level (validator enforces the cap, but the planner should prefer flat).
- **Schema reminder:** `subplan.nodes` follows the same shape as the top-level
  plan; the subplan's final node's output becomes this node's output.
- **Example pair:** one well-formed subplan node and one over-nested anti-pattern.

**Acceptance:** add an eval task `subplan-correct` to the dagagent suite that
requires the planner to emit a subplan for a naturally partitioned research
task and verify with a trajectory criterion (`expect_node_type = "subplan"`).

---

## 2. Planning failure mode detection

**Type:** Code — planner + orchestrator.

**Problem.** Two failure modes currently have no explicit handling:

1. **Streaming stall.** The model starts generating but never emits valid JSON
   (thinking token runaway, fence-wrapped output that `strip_model_output`
   can't recover, partial JSON). The executor sees a parse error, but the
   *reason* is lost and the retry starts cold.
2. **Silent over-rejection.** The planner can emit `{"rejection": "..."}` for
   tasks it can't handle, and the orchestrator surfaces this correctly — but
   the planner also sometimes rejects tasks it *should* handle (e.g.
   predictions it should decline gracefully, knowledge questions with no
   required tool). The distinction between a *legitimate* rejection and a
   *mistaken* one is invisible at runtime.

**Changes.**

### 2a. Stall detection and warm retry

In `src/dagagent/planner/planner.py`, the streaming loop accumulates raw
chunks into `raw`. Add a token-budget check: if the accumulated length exceeds
`settings.budgets.planner * 4` characters (a generous proxy for token count)
without a successful parse, mark the attempt as `error_type="stall"` and
preserve the accumulated text in `error_detail`.

On the next attempt, inject the stall content into the prompt:

```
A previous planning attempt produced output that could not be parsed as a
valid plan. The raw output was:

<previous_attempt>
{error_detail[:2000]}
</previous_attempt>

Do not repeat this output. Emit only a valid JSON plan or a rejection object.
```

This is distinct from the existing guided-replan path (user feedback) — it's
an automatic warm retry for parse failures.

### 2b. Rejection classification

When the planner emits a rejection, log it at `WARNING` with a structured
field `rejection_reason` so operators can distinguish legitimate rejections
(unknown tool, contradictory request) from mistaken ones (prediction /
knowledge / no-tool tasks that should have been routed to a result node).

No change to external behavior — just better observability. Consider adding a
`rejection_class: "legitimate" | "suspected_mistaken"` heuristic based on
whether the task prompt contains keywords that the updated planner prompt
explicitly says to route to a result node (see prompts.py).

**Relevant files:** `src/dagagent/planner/planner.py` (streaming loop,
`PlanningAttempt`, retry injection), `src/dagagent/config/prompts.py`
(warm-retry prompt fragment), `src/dagagent/config/settings.py` (stall budget
multiplier as a config knob).

**Acceptance:** `calibrate-unknowable` passes reliably (tracks prompt fix
from 97128be + rejection logging). Add a unit test in `tests/unit/` that
feeds a scripted provider returning >budget characters of non-JSON and asserts
the attempt is marked `error_type="stall"` with `error_detail` populated.

---

## 3. Upstream context awareness

**Type:** Code — executor context assembly. Small, well-scoped.

**Problem.** `assemble_context` in `src/dagagent/executor/context.py` already
accepts a `dependent_nodes` argument, but only `_execute_subplan` passes it
(line 486 of executor.py). All other node types — including synthesis and
summary, which most benefit from knowing what downstream nodes need — receive
`dependent_nodes=None`.

A synthesis node aggregating three research results doesn't know whether the
downstream result node needs a structured comparison, a recommendation, or a
plain summary. It defaults to a general synthesis. Passing dependent
descriptions lets it tailor its output.

**Change.** In `src/dagagent/executor/executor.py`, for every call to
`assemble_context` outside `_execute_subplan`, resolve the direct dependents
of the current node from the plan and pass them:

```python
dependents = [
    n for n in state.plan.nodes
    if node.id in n.context_needed
]
ctx = assemble_context(node, state.results, dependent_nodes=dependents or None)
```

The `assemble_context` function already formats these as a
"Your output will be consumed by:" block — no changes needed there.

**Relevant files:** `src/dagagent/executor/executor.py` (all
`assemble_context` call sites except the subplan one),
`src/dagagent/executor/context.py` (no change needed).

**Scope guard.** Do not change `assemble_context`'s signature or the
fresh-context invariant. Dependent descriptions are metadata only — they must
not carry upstream *results* into a node's context.

**Acceptance:** add an integration test in `tests/integration/` that runs a
two-node plan (synthesis → result) with a scripted provider and asserts the
synthesis node's prompt contains the downstream result node's description.

---

## 4. Summary node format variants

**Type:** Code — core model + executor. Small.

**Problem.** All summary nodes use the same prompt and temperature regardless
of their role. A summary that feeds a user-facing result needs to be more
complete than one that only gates a decision branch. There is no way for the
planner to express this intent.

**Change.**

### 4a. Add `format` field to `Node`

In `src/dagagent/core/plan.py`:

```python
class SummaryFormat(StrEnum):
    CONCISE = "concise"   # 1–3 sentences; gates a decision or feeds another node
    MEDIUM  = "medium"    # default; balanced paragraph
    VERBOSE = "verbose"   # full detail; feeds a user-facing result directly
```

Add `format: SummaryFormat = SummaryFormat.MEDIUM` to `Node`. Only
meaningful when `type == NodeType.SUMMARY`; ignored otherwise.

### 4b. Route through `_LLM_NODE_SPECS`

`_LLMNodeSpec` in `executor.py` currently holds a fixed temperature. Extend it
or add a lookup: when executing a summary node, select the system prompt
fragment based on `node.format`. Three prompt fragments in `prompts.py`:

- `SUMMARY_CONCISE_SYSTEM` — "Produce a concise 1–3 sentence summary…"
- `SUMMARY_MEDIUM_SYSTEM` — existing `DEFAULT_SUMMARY_SYSTEM`
- `SUMMARY_VERBOSE_SYSTEM` — "Produce a thorough summary preserving all key
  facts, named entities, and quantitative results…"

### 4c. Planner prompt

Add `format` to the schema comment in `DEFAULT_PLANNER_SYSTEM` for summary
nodes. Tell the planner: omit for default; set `"concise"` when the summary
feeds a decision condition or a downstream node that needs only a signal; set
`"verbose"` when the summary feeds the final result directly.

**Relevant files:** `src/dagagent/core/plan.py`, `src/dagagent/config/prompts.py`,
`src/dagagent/executor/executor.py` (`_LLM_NODE_SPECS` and
`_execute_llm_node`), `src/dagagent/validator/validator.py` (no change needed
— unknown fields are already ignored).

**Acceptance:** unit test that a `Node(type="summary", format="concise")` and
`Node(type="summary", format="verbose")` produce different system prompts in
the executor. Eval signal: `summarize-doc-long` pass rate — verbose summary
nodes should preserve named entities better than the current medium default.

---

## 5. Per-tool HITL configuration *(needs design — not buildable yet)*

**Type:** Code + design. The implementation is straightforward once the
policy schema is agreed; the design questions are listed below.

**Current state.** `ToolRisk` (`read` / `write` / `execute`) is declared per
tool at registration time and is already stored in `ToolHarness._risk`. The
executor's HITL gate currently fires only for plan approval (ACP
`session/request_permission`). There is no per-tool approval step.

**Goal.** Before invoking a tool node, the executor checks the tool's approval
policy. If the policy requires user confirmation, it pauses via the existing
`session/request_permission` primitive and proceeds or aborts based on the
response.

**Open design questions (must be answered before building):**

1. **Policy schema.** Three options:
   - *Risk-threshold:* "require approval for any tool with risk ≥ X" — simple,
     one config knob, but coarse.
   - *Per-tool override:* each tool registration accepts an optional
     `approval: "auto" | "always" | "never"` field that overrides the
     risk-based default — fine-grained, but adds noise to most registrations.
   - *Hybrid:* global risk threshold + per-tool override list. Recommended.
2. **Where config lives.** In `dagagent.toml` / env (operator-level), or in
   the tool registration itself (developer-level), or both?
3. **`auto` mode decision.** If policy is `auto`, what decides whether to
   prompt? Current `ToolRisk` values (`read`/`write`/`execute`) are a natural
   signal (`read` → no prompt; `write`/`execute` → prompt). Is that sufficient
   or does context (e.g. the specific args extracted) need to factor in?
4. **Interaction with plan HITL.** If the user already approved the plan and
   the plan contains a `write` tool node, is per-tool approval redundant?
   Suggested answer: plan approval covers intent; per-tool approval covers the
   specific extracted args (what file, what content). They serve different
   purposes and should be independent.
5. **ACP surface.** Does per-tool approval use the existing
   `session/request_permission` method with a different payload, or a new
   ACP method? Suggested: same method, new `kind: "tool_call"` field alongside
   the existing `kind: "plan_approval"`.
6. **Non-ACP surfaces (REST gateway, CLI).** What happens when there is no
   HITL channel? Options: abort with an error, auto-approve, auto-deny. Needs
   a default.

**Tentative implementation plan** (once design is resolved):

1. Extend `ToolHarness.register` with `approval: Literal["auto","always","never"] = "auto"`.
2. Add `hitl_policy_for(name) -> ApprovalPolicy` to `ToolHarness`.
3. In `Executor._execute_tool_node`, after arg extraction and before tool
   invocation, call `hitl_policy_for(tool_name)`. If approval required, call
   the existing HITL gate with a `"tool_call"` payload containing tool name
   and resolved args. On deny, mark the node `NodeStatus.FAILED` with a
   `user_denied` error; do not abort the whole plan (let the executor handle
   the downstream consequence naturally).
4. Update the ACP server's `request_permission` handler to accept `kind:
   "tool_call"` and surface the tool name + args to the client.

**Acceptance:** integration test that a `write`-risk tool with `approval="always"`
causes the executor to call the HITL gate before invocation, and that a
`read`-risk tool with `approval="auto"` does not.
