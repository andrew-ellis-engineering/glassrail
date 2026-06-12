# Spec: Parallel node execution

Status: Proposed
Priority: P1 — first engine workstream. Prerequisite for `foreach` (vault spec);
`foreach`'s value is parallel fan-out and must not land on a sequential engine.
Depends on: nothing. Part B is separately mergeable after Part A.

## Purpose

`Executor._run` in `src/glassrail/executor/executor.py` currently executes
nodes **strictly sequentially**: a plain `for node_id in plan.sorted_node_ids`
loop awaiting one node at a time. Independent nodes — e.g. the N×M tool fan-out
of a comparison task — are serialised even when they could run concurrently
against cloud tiers. Public docs previously claimed "same layer = runs in
parallel"; those claims have been corrected to "dependency layers" until this
spec lands. This spec makes the executor actually run independent ready nodes
concurrently, formalises skip-propagation semantics, and (Part B) makes
subplan execution visible on the event stream.

## Invariants that must survive (read before designing anything)

- **Fresh context per node** — `assemble_context` reads only `context_needed`;
  the property suite `tests/property/test_fresh_context.py` must pass untouched.
- **DAG acyclicity** — no change to plan grammar.
- **Dependency order** — a node never starts before every node it depends on
  (data dep via `context_needed`, control dep via a governing decision) has a
  recorded result.
- **Final-output selection** is unchanged: it scans `plan.sorted_node_ids` (a
  static list), not completion order.
- stdlib `asyncio` only; pyright strict.

## Part A — Ready-set scheduler

### Design

Replace the sequential loop with a ready-set scheduler:

1. **Dependency map.** Build once per plan:
   `deps(node) = set(node.context_needed) ∪ {d.id for every DECISION node d
   where node.id appears in any of d.branches' target lists}`. The second term
   is the control dependency — branch targets must not start before their
   governing decision resolves. (The validator already orders branch targets
   after their decision in `sorted_node_ids`; this makes the constraint explicit
   for the scheduler.)
2. **Readiness.** A node is *ready* when every id in `deps(node)` has an entry
   in `state.results` (any status — completed, failed, empty, or skipped all
   resolve a dependency; this preserves today's behaviour where dependents of a
   failed node run and see the failure notice from `assemble_context`).
3. **Skip propagation (formalised — this resolves the open question now pinned
   by the `harness-mechanics` `skip-transitive` task):**
   - When a decision completes, the targets of every untaken branch are marked
     skipped and get a `SKIPPED` `NodeResult` recorded immediately (existing
     `_record_branch_decision` behaviour).
   - **New transitive rule:** when a node becomes ready, it is auto-skipped
     (recorded `SKIPPED`, `NodeFinished` emitted) iff it has at least one
     non-decision dependency in `context_needed` **and** all of its
     non-decision `context_needed` dependencies are `SKIPPED`. A node with a
     mix of completed and skipped deps runs (join nodes must run). A node with
     no `context_needed` never auto-skips. This mirrors the existing
     `_only_uses_skipped_content` predicate used for final-output selection —
     reuse/extract that logic rather than duplicating it.
4. **Dispatch loop.** Maintain `pending` (ids without results), dispatch every
   ready node as an `asyncio` task inside an `asyncio.TaskGroup`, bounded by an
   `asyncio.Semaphore(settings.max_concurrent_nodes)`. After each node task
   completes (writes its `NodeResult`, emits `NodeFinished`), re-evaluate
   readiness of remaining pending nodes and dispatch newly ready ones. Loop
   until `pending` is empty. A simple implementation: a coordinator that
   `await`s `asyncio.wait(..., return_when=FIRST_COMPLETED)` over in-flight
   node tasks; correctness over cleverness.
5. **New setting:** `max_concurrent_nodes: int = 4` on `Settings`
   (`src/glassrail/config/settings.py`), env `GLASSRAIL_MAX_CONCURRENT_NODES`.
   Document in the README configuration table with the caveat that a local
   tier-0 server often serves one sequence at a time (the launchd MLX config
   uses `--max-num-seqs 1`), so concurrency mainly benefits cloud tiers.
   `max_concurrent_nodes = 1` must reproduce exactly today's sequential
   behaviour and event ordering — keep that property; it is the escape hatch.

### Consequences to handle explicitly

- **Result/state mutation** is safe (single event loop, no threads), but
  `state.completed_nodes` becomes completion-ordered rather than
  topo-ordered. `grep -rn "completed_nodes" src/ tests/` and confirm no
  consumer assumes topological order; the CLI trajectory must **not** use it —
  see next bullet.
- **Trajectory ordering:** the `glassrail run --json` envelope's `trajectory`
  (built in `src/glassrail/cli/__init__.py`) must be emitted in
  `plan.sorted_node_ids` order regardless of completion order, so eval
  trajectory criteria (`expect_before`/`expect_after`, ordered
  `tool_sequence`) keep meaning *dependency* order. Enforce and test this.
- **Eval update:** the `harness-mechanics` task `order-topological` pins
  execution order. Read its `config.toml`; rewrite its criteria to assert
  dependency-edge order only (pairs connected by an edge), not total order.
  Update the `skip-transitive` task to pin the **new** transitive-skip
  semantics from §3, and add a `skip-join-runs` task pinning that a node with
  one completed and one skipped dep still runs. (Suite-content changes — no
  `HARNESS_VERSION` bump.)
- **Events** now interleave across nodes. The Python TUI (`gateways/tui/view.py`)
  and ACP adapter key everything by `node_id` / `PlanTracker` internally —
  verify, don't assume; the ACP plan-entry list order derives from the static
  plan, which is unchanged.
- **OTel spans:** node spans are currently opened sequentially with
  `start_as_current_span`. Each dispatched node task inherits the task-span
  context via contextvars at task-creation time — open the node span *inside*
  the node task so LLM-call spans nest correctly. Update
  `tests/integration/test_telemetry.py` to assert span *parentage*, not
  emission order.
- **Tool approval:** multiple concurrent `ASK` nodes produce concurrent
  `ToolApprovalBroker.request` futures — the broker already supports this
  (keyed by approval id). No change; add a test.
- **Node failure semantics are unchanged:** a failed node does not abort the
  task; dependents run with the failure notice. (Task-level fail-fast is out
  of scope — note it as a possible future `[resilience]` flag.)
- **Subplans** call `_run` recursively; the nested run gets its own scheduler
  with the same semaphore limit (do not multiply concurrency by nesting —
  simplest correct approach: pass the parent's semaphore down).

### Tests (Part A)

- Unit (`tests/unit/test_executor.py`): a diamond plan (1 → 2,3 → 4) with a
  *barrier* fake provider — both branch calls must be in flight simultaneously
  before either is released (e.g. the provider counts entrants and only
  releases an `asyncio.Event` when 2 are waiting; guard with
  `asyncio.wait_for` so a regression to sequential execution fails fast
  instead of deadlocking). A mirror test with `max_concurrent_nodes=1`
  asserting strict sequential order.
- Unit: transitive-skip — decision skips node A; node B with
  `context_needed=[A]` is auto-skipped; node C with `context_needed=[A, X]`
  (X completed) runs.
- Existing executor/orchestrator/property tests pass unmodified except where
  they assert total execution order — fix those to assert dependency order.
- Integration: full orchestrator run over a fan-out plan; final output and
  event set identical to the sequential baseline.

## Part B — Subplan event visibility (separately mergeable)

Today `_execute_subplan` runs the nested plan with `emit=False`: subplan
internals are invisible on every surface (REST/SSE, ACP, both TUIs), and
`foreach` would inherit that blindness.

### Design

- Add `node_path: str | None = None` to `NodeStarted`, `NodeFinished`,
  `NodeOutputChunk`, and `BranchDecided` in `src/glassrail/events/types.py`.
  `None` (or omitted) means top-level. Nested nodes get
  `f"{parent_node_id}"`-prefixed slash paths (node 2 inside subplan node 4 →
  `"4/2"`; one level of nesting today, but the scheme composes). The `node_id`
  field keeps the *local* integer id. Additive field — existing JSON consumers
  ignore it.
- `_run` gains a `path_prefix: str = ""` parameter; `_execute_subplan` calls it
  with `emit=True` and `path_prefix=str(node.id)`.
- **Consumer policy v1:** the ACP adapter (`gateways/acp/server.py`) and the
  Python TUI (`gateways/tui/view.py`) *filter out* events whose `node_path`
  contains `/` — their rendering is unchanged by this spec. REST/SSE/WS
  consumers receive nested events; document the field in `docs/streaming.md`.
- Subplan child `ExecutionState` task-id isolation is a separate item
  ([small-fixes](small-fixes.md) item 7) — do not bundle.

### Tests (Part B)

- Integration: a plan with a subplan emits nested `NodeStarted`/`NodeFinished`
  with `node_path="<parent>/<child>"`, and the parent subplan node still emits
  its own start/finish without a path.
- ACP adapter test: notifications are byte-identical to the pre-change
  behaviour for a plan containing a subplan (the filter works).

## Acceptance criteria

- Full check sweep green; `mkdocs build --strict` green (streaming.md edit).
- `uv run python3 eval-framework/run.py suite eval-framework/suites/harness-mechanics`
  green, including the updated `order-topological`, updated `skip-transitive`,
  and new `skip-join-runs` tasks.
- Barrier test proves ≥2 nodes in flight; `max_concurrent_nodes=1` reproduces
  sequential behaviour.
- One confirmation run of `suites/glassrail-openrouter` at or above current
  results (concurrency must not change answers, only wall time).
- README: `max_concurrent_nodes` row added; the "dependency layers" phrasing
  may now honestly say layers run in parallel — update README and
  `docs/tui.md` wording in the same change.
- CHANGELOG entry under `[Unreleased]`.
