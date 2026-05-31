# Spec: node-level harness evals

Status: **proposed** · Audience: dagagent + eval-framework maintainers · Companion
to `suites/dagagent/EVAL_PLAN.md` (end-to-end) and `docs/evals.md` (how to run).

---

## 1. The gap this closes

The `dagagent` suite today evaluates **request → final answer**. That measures
*model + harness in aggregate*. It cannot localize a harness defect: if
fresh-context leaks, a branch skips the wrong nodes, the wrong `result` node is
chosen as the final output, or extracted tool args are dropped, the only symptom
is a worse final answer — indistinguishable from the model having a bad day.
Catching those through end-to-end tasks would need an impossibly elaborate suite
and still wouldn't *pin* the cause.

We want two distinct things, and the current suite conflates them:

| Layer | Question | Stochastic? | Suite intent |
|---|---|---|---|
| **Harness mechanics** | Does the *execution model* behave exactly as specified? | No — deterministic given inputs | **regression from day 1** |
| **Per-node model behavior** | Does the model drive *one node type* correctly, in isolation? | Yes | capability → regression via ratchet |
| End-to-end (existing) | Does model+harness solve a whole task? | Yes | capability → regression |

This spec defines the first two layers. The core move is the same for both:
**isolate a node** — drive a single node type (or a minimal plan that exercises
one mechanism) with controlled inputs and assert its behavior precisely. The two
layers differ only in whether the model under the node is **scripted**
(deterministic, model-independent → pure harness regression) or **real**
(multi-trial → model+harness capability).

---

## 2. What the harness actually does (the contract under test)

Grounded in `src/dagagent/executor/executor.py`. These are the behaviors the
node-level suite must pin. Citations are to the current implementation so the
assertions track real code, not an idealized model.

### 2.1 Dispatch & per-node-type behavior

- **TOOL** (`_execute_tool`): `tool is None` → `FAILED`. Uses `node.args_template`
  verbatim when present; **only** when context exists *and* no template is set
  does it call `_extract_args` (an LLM micro-call). Tool output of `None`, `{}`,
  or `[]` → status `EMPTY`. Otherwise runs `_check_output_shape` (LLM gate);
  a mismatch → `COMPLETED` but `flagged=True, confidence=0.5`. Clean →
  `COMPLETED, confidence=1.0`.
- **DECISION** (`_execute_decision`): parses `{branch, confidence}`. Unknown
  branch → falls back to `node.default_branch`. Any parse/transport failure →
  `default_branch`, `confidence=0.0`. Always followed by `_record_branch_decision`,
  which adds every **non-taken** branch's node ids to the `skipped` set and
  appends a `BranchLogEntry`.
- **SYNTHESIS / THINK / SUMMARY / RESULT** (`_execute_llm_node`, table
  `_LLM_NODE_SPECS`): one LLM call; reads one JSON field —
  `output`/`reasoning`/`summary`/`output` respectively — into `result.output`;
  default confidence `0.9/0.7/0.9/0.9`; invalid JSON / exception → `FAILED`.
- **SUBPLAN** (`_execute_subplan`): `subplan is None` → `FAILED`. Runs the nested
  plan in a **fresh `ExecutionState`** with `emit=False` (nested node ids must
  not leak onto the parent event stream). Nested `final_output` bubbles up as the
  node's output; a nested run with no `final_output` → `EMPTY`.

### 2.2 Cross-cutting execution-model invariants

- **Fresh context** (`assemble_context`): a node sees *only* the outputs of the
  ids in its `context_needed` — nothing else.
- **Topological order**: nodes run in `plan.sorted_node_ids`.
- **Deterministic tier selection** (`_select_tier`): `forced_tier` wins; else
  DECISION→0, TOOL→0, THINK→2, `reasoning_required`→2, else→0. *The model never
  picks the tier.*
- **Branch skip propagation**: non-taken branch nodes become `SKIPPED`; a skipped
  node emits **`NodeFinished(SKIPPED)` but no `NodeStarted`** (see the skip path
  in `_run`).
- **Confidence flagging**: `COMPLETED` with `confidence < confidence_threshold`
  (default 0.75) → `flagged=True`.
- **Final-output selection** (`_extract_final_output`): the **last completed
  `RESULT`** node's output; if none, fall back to the last completed `SYNTHESIS`;
  if neither, `final_output is None`.
- **Event stream**: `NodeStarted`→`NodeFinished` per executed node,
  `BranchDecided` after each decision, one terminal `TaskCompleted`.

### 2.3 Two real gaps this work must also fix

1. **Trajectory hides actual tool args.** `cli/__init__.py:_trajectory` sets
   `"input": node.args_template or {}`. When args come from `_extract_args`, the
   envelope reports `{}` — so *no eval can see what was actually sent to the
   tool*. The `tool-args-path` end-to-end task is effectively ungraded on its
   core claim today. Fix: record the resolved args on `NodeResult` and surface
   them (§4.3).
2. **Per-node outputs are not surfaced.** Only `final_output` is in the envelope.
   Asserting a `summary`/`think` node's own output requires exposing it (§4.3).

---

## 3. Design principles for this suite

1. **Determinism first.** Anything assertable with a scripted model goes in the
   harness-mechanics suite and is graded **deterministically** — no judge, no
   multi-trial. `pass^1 == pass@1`; a single trial is a proof, not a sample.
2. **One mechanism per task.** A task isolates exactly one behavior (e.g. "a
   decision that returns `no` skips the `no`-branch's nodes"). When it fails, the
   task name *is* the diagnosis.
3. **Scripted model = model-independent.** Harness-mechanics tasks must produce
   identical results on any backend, because the model's outputs are fixed
   inputs. This is what makes them a true *harness* regression suite.
4. **No planner in the loop.** These tasks inject a fixed plan. Planner quality is
   a separate concern (its own tasks, §6.5), kept out so a planner regression
   can't masquerade as an executor regression.
5. **Don't duplicate pytest.** Invariants that need *in-process introspection of
   internal state* (exact assembled-context string, no-leak property over random
   plans) stay in `tests/property` / `tests/integration` and are referenced, not
   reimplemented. The eval-framework asserts only what is **observable in the
   envelope**. §7 draws the line.

---

## 4. Enabling infrastructure (dagagent changes)

Four additions. All small; (a) and (b) are the load-bearing ones.

### 4.1 `dagagent exec-plan` — run a fixed plan, skip planning

A new CLI command that reads a plan JSON (file arg or stdin), builds an
`ExecutionState` with that plan, runs **only** the executor, and emits the *same*
`--json` envelope as `run`. The orchestrator already executes a pre-built plan on
the `resume` path (`executor.execute(state)`), so this is a thin entry point.

```bash
dagagent exec-plan plan.json --json            # validate + execute, emit envelope
dagagent exec-plan plan.json --json --no-validate   # negative tests: feed an invalid plan
```

`--no-validate` lets us assert executor-level error handling for malformed plans
(e.g. a TOOL node with no tool) without the validator rejecting them first.

### 4.2 A `scripted` provider — deterministic model responses

A new provider kind selectable per tier, so harness-mechanics runs are
model-free. It replays canned responses (the existing `_Scripted` test pattern,
promoted to a real provider): an ordered JSONL file where each line is the raw
string the model would have returned for the next LLM call.

```toml
# resolved from env for the eval subprocess
DAGAGENT_TIER0__KIND = "scripted"
DAGAGENT_TIER0__SCRIPTED_PATH = "/abs/path/responses.jsonl"
```

```jsonl
{"branch": "no", "confidence": 0.95}
{"output": "final answer text", "confidence": 0.9}
```

Responses are consumed in **call order**. Because tier selection and dispatch are
deterministic, call order is stable for a fixed plan — the fixture author can
predict the sequence. (Open question §8.1: ordered-list vs. a matcher keyed on
node id, if call order proves brittle.)

### 4.3 Envelope enrichments (also fixes §2.3)

Add to each trajectory step, and record the source fields on `NodeResult`:

- `args_used` — the dict actually passed to the tool (template **or** extracted),
  distinct from the planned `input`.
- `output` — the node's own output, stringified and truncated (cap, e.g., 2 KB)
  so large summaries don't bloat artifacts. Enables per-node output assertions.

These are additive, backward-compatible, and immediately improve the existing
end-to-end suite (the `tool-args-path` claim becomes real).

### 4.4 eval-framework wiring

- **Subject**: extend `dagagent-cli` with a `mode = "exec-plan"` backend-config
  flag that swaps argv from `run <prompt>` to `exec-plan <plan_fixture>` and
  installs the scripted responses path into the subprocess env. No new subject
  class; reuse capture/timeout/env machinery added in harness 0.2.1.
- **Task fields** (new, optional):
  - `plan = "plan.json"` — the injected plan fixture (under the task's `fixtures/`).
  - `scripted = "responses.jsonl"` — canned model responses (harness-mechanics only).
  - `validate = true|false` — pass `--no-validate` when false.
- **Grader vocabulary** (new node-targeted checks, §5).

---

## 5. New grader vocabulary (node-targeted)

The deterministic and trajectory graders gain a `node_id` selector so a criterion
can address one node in the trajectory. All evaluated against the envelope —
still 100% deterministic.

```toml
# trajectory grader, addressed to a specific node
[[criteria]]
text = "Decision node 2 took the 'no' branch"
grader = "trajectory"
node_id = 2
expect_branch = "no"

[[criteria]]
text = "Node 3 (no-branch body) was skipped"
grader = "trajectory"
node_id = 3
expect_status = "skipped"

[[criteria]]
text = "Think node 4 ran at tier 2"
grader = "trajectory"
node_id = 4
expect_tier = 2

[[criteria]]
text = "Tool node 1 was called with the path from context"
grader = "trajectory"
node_id = 1
expect_args_contains = "/tmp/dagagent-eval/app.conf"   # checks args_used

# deterministic grader on a single node's output
[[criteria]]
text = "Summary node 5 preserved the planted figure"
grader = "deterministic"
check = "regex"
target = "node:5"            # new target selector → that node's `output`
value = "\\b42%"
```

New selectors: `expect_status`, `expect_tier`, `expect_branch`, `expect_flagged`,
`expect_args_contains` on the trajectory grader; a `node:<id>` target on the
deterministic grader (alongside the existing `__result_text__`). Each maps
directly onto envelope fields — no model call.

---

## 6. The node-by-node test matrix

`HM` = harness-mechanic (scripted model, deterministic, regression).
`MH` = model+harness (real model, multi-trial, capability→regression).
Each `HM` row is one isolating task with a fixed plan + scripted responses.

### 6.1 TOOL node

| id | layer | plan / script | asserts |
|---|---|---|---|
| `tool-template-args` | HM | 1 TOOL w/ `args_template` | tool called with **exactly** the template; `args_used == template`; tier 0; `COMPLETED` conf 1.0 |
| `tool-empty-output` | HM | TOOL → tool returns `{}` | status `EMPTY` (not COMPLETED, not FAILED) |
| `tool-missing-name` | HM | TOOL `tool=None`, `--no-validate` | `FAILED`, error "TOOL node has no tool name" |
| `tool-shape-flag` | HM | TOOL + scripted shape-gate = mismatch | `COMPLETED`, `flagged`, conf 0.5 |
| `tool-extract-args` | MH | TOOL w/ context, no template | `args_used` contains the correct value extracted from context (real model); replaces the blind `tool-args-path` |

### 6.2 DECISION node + branch propagation

| id | layer | plan / script | asserts |
|---|---|---|---|
| `decision-skip-untaken` | HM | DECISION(yes/no) → script `no` | `no`-branch nodes `SKIPPED`; `yes`-branch nodes run; `BranchDecided` = `no`; branch_log entry present |
| `decision-skip-no-start` | HM | as above | skipped nodes emit `NodeFinished(SKIPPED)` and **no** `NodeStarted` |
| `decision-unknown-branch` | HM | script returns `branch="maybe"` | falls back to `default_branch` |
| `decision-parse-failure` | HM | script returns invalid JSON | `default_branch`, confidence 0.0 |
| `decision-tier0` | HM | any decision | tier 0 |
| `decision-correct-yes` / `-no` | MH | real model, context implying each branch | picks the correct branch (control pair) |

### 6.3 SYNTHESIS / THINK / SUMMARY / RESULT

| id | layer | plan / script | asserts |
|---|---|---|---|
| `llm-output-key-{synthesis,summary,think,result}` | HM | 1 node, script `{<key>: "X", confidence: 0.9}` | correct field extracted into `output` (think reads `reasoning`, summary reads `summary`, …) |
| `llm-default-confidence` | HM | script omits `confidence` | per-type default applied (0.7 think / 0.9 others) |
| `llm-flag-low-confidence` | HM | script `confidence: 0.5` | `flagged=True` |
| `llm-invalid-json` | HM | script returns non-JSON | `FAILED` |
| `think-tier2` / `synthesis-tier0` | HM | one of each | tier from the deterministic table |
| `summary-fidelity-node` | MH | SUMMARY fed a doc as upstream context | needles survive in node `output` (isolates the summary node from planning) |

### 6.4 SUBPLAN node

| id | layer | plan / script | asserts |
|---|---|---|---|
| `subplan-bubbles-output` | HM | SUBPLAN w/ nested 1-node plan | parent node `output == nested final_output` |
| `subplan-id-isolation` | HM | nested ids collide with parent ids | parent trajectory/events contain **no** nested node ids |
| `subplan-missing` | HM | SUBPLAN `subplan=None`, `--no-validate` | `FAILED` |
| `subplan-empty` | HM | nested plan yields no final_output | `EMPTY` |

### 6.5 Cross-cutting execution-model tasks

| id | layer | asserts |
|---|---|---|
| `order-topological` | HM | trajectory node order == `sorted_node_ids` for a diamond DAG |
| `final-output-last-result` | HM | two RESULT nodes → final == the **last** completed one |
| `final-output-synthesis-fallback` | HM | plan with no RESULT → final == last SYNTHESIS |
| `final-output-none` | HM | plan with neither → `final_output` null, status still COMPLETED |
| `fresh-context-observable` | HM | node B's `output` (scripted to echo its context) reflects only `context_needed` ids — the *envelope-observable* slice of the fresh-context property |
| `skip-transitive` | HM | **decision point** (§8.2): does a node downstream of a skipped branch, but not itself listed in the branch, run or skip? Pin current behavior, then decide if it's correct |

---

## 7. What stays in pytest (no duplication)

The eval-framework only sees the envelope. These belong in-process and are
*referenced* by this suite, not reimplemented:

- **Fresh-context no-leak property** over randomized plans — `tests/property`
  already asserts no out-of-context content leaks into assembled prompts. The
  `fresh-context-observable` eval is a coarse, black-box echo of it for
  regression visibility, not a replacement.
- **Event-bus ordering/pairing** at the object level — `tests/integration`
  (`test_orchestrator_events`, `test_rest_events`). The suite asserts the
  *envelope's* derived trajectory, which is downstream of those.
- **Validator invariants** (topo sort, cycle detection, subplan caps) — unit
  tests in `tests/unit`. The `--no-validate` eval tasks deliberately *bypass* the
  validator to reach executor error paths; they are not validator tests.

Rule of thumb: if the assertion needs a Python object the envelope doesn't carry,
it's a pytest test. If it's observable in `--json`, it's an eval.

---

## 8. Open decisions (need a call before building)

1. **Scripted response keying.** Ordered JSONL (simplest, but brittle if dispatch
   order shifts) vs. a matcher keyed on `node_id` or a request substring (robust,
   more machinery). Recommendation: start ordered; add node-id keying only if a
   fixture proves fragile.
2. **`skip-transitive` semantics.** Today `_record_branch_decision` only skips
   nodes *listed in* the non-taken branches. A node downstream of a skipped node
   but not itself in the branch will still run (and its `context_needed` upstream
   will be `SKIPPED`/absent). Is that intended? This task will surface the current
   behavior; we then decide whether it's a spec or a bug. **Likely a real latent
   bug — worth resolving as part of this work.**
3. **Suite layout.** Proposed: `suites/harness-mechanics/` (HM, all `regression`,
   `--trials 1`) and `suites/node-capability/` (MH, capability→ratchet). Keeps the
   deterministic regression wall separate from the stochastic capability set.
4. **Promotion.** HM tasks are deterministic, so the 5-clean-run ratchet is
   overkill — they can be `regression` on creation. Confirm we want to bypass the
   ratchet for the HM suite.

---

## 9. Build sequencing

1. **Infra:** `exec-plan` command + envelope enrichments (`args_used`, per-node
   `output`). Immediately fixes the `tool-args-path` blind spot in the existing
   suite. Bump `HARNESS_VERSION`.
2. **Scripted provider** + the `mode = "exec-plan"` subject wiring + node-targeted
   grader selectors.
3. **HM suite** (§6.1–6.4 harness-mechanic rows + §6.5) — the deterministic
   regression wall. All `regression`, `--trials 1`.
4. **MH suite** (the capability rows) — isolated per-node model behavior,
   multi-trial, control-paired where applicable.
5. **Resolve `skip-transitive`** (§8.2) and any defects the HM suite surfaces.

The HM suite is the high-value first deliverable: it is fast (no model, no
judge), fully reproducible, and turns every execution-model guarantee in §2 into
a named, CI-blocking check.
