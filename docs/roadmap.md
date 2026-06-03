# Roadmap

*Stub — full roadmap lives in the design vault and is being ported here.*

## Phase 0 — Prototype (done)

Single-file FastAPI prototype. Validated DAG planning + tier routing + audit logging end to end. Now superseded by the package.

## Phase 0.5 — OSS Foundation (complete)

- Package restructure (src-layout, subpackages) ✓
- Pluggable LLM provider interface (`Protocol`, streaming) ✓
- Pluggable state store interface ✓
- HITL approval primitive ✓
- State persistence (SQLite) ✓
- Extended node types: `think`, `summary`, `result`, `subplan` ✓
- Property tests for the fresh-context invariant ✓
- Typed event stream + SSE endpoint ✓

## Phase 1 — Reliability & Eval

Make the engine observable, measurable, and shippable. Suggested order:
eval harness first (it gates everything else and the PyPI publish), then
observability, then the operational surfaces.

- **Eval framework** ✓ — the standalone, stdlib-only `eval-framework/` runs each
  task *k* times against a pluggable subject backend and reports pass@k
  (capability) vs pass^k (reliability) with a deterministic → trajectory → LLM
  grading cascade, control pairs, and a promotion ratchet. The `dagagent-cli`
  backend drives the real planner + executor over the agent's own tier routing
  (your MLX model) via `dagagent run --json`. See [Evals](evals.md). The release
  gate below — promoting capability tasks to regression at an agreed pass^k bar —
  is still open.
- **OpenTelemetry GenAI spans** ✓ — the planner, router, and executor emit a
  span tree (task → plan / node → LLM call) with GenAI semantic-convention
  attributes (model, tier, tokens) plus `dagagent.*` ones. Tracing is a no-op
  until configured; the SDK + OTLP exporter live in the optional `otel` extra.
  See [Observability](observability.md).
- **WebSocket event transport** ✓ — `WS /task/{id}/events` is a second
  consumer of the existing `EventBus` alongside SSE: it streams the same typed
  events and closes on a terminal event, sharing one transport-agnostic event
  source. Producers (executor/orchestrator) are unchanged. See
  [Streaming events](streaming.md).
- **Docker production image** ✓ — a multi-stage `Dockerfile` serves the REST
  gateway from a slim (~60 MB), non-root `python:3.12-slim` image with a
  built-in health check. CI builds and smoke-tests it on every change. See
  [Deployment](deployment.md).
- **TUI** ✓ — `dagagent tui "<request>"` submits a task to a running gateway
  and renders the live SSE stream with Rich: plan → per-node progress → final
  output (a terminal snapshot if it connects after the task finished). See
  [Terminal UI](tui.md).
- **ACP adapter + Rust client** ✓ — `dagagent acp` exposes the agent over the
  Agent Client Protocol (JSON-RPC 2.0 on stdio), and the in-repo Rust
  `clients/tui` client drives it: submit a task, stream the plan and nodes,
  approve or reject-with-feedback the plan (guided replan), dovetail follow-up
  tasks, and cancel a run — all without a running gateway. Deferred to later:
  token-level streaming, session persistence/`session/load`, structural plan
  editing, and additional ACP clients (desktop/IDE).
- **Planner subplan guidance** — add explicit instruction to the planner system
  prompt covering when and how to emit a `subplan` node, with examples. No code
  change; pure prompt improvement.
- **Planning failure mode detection** — the planner can reason indefinitely
  without emitting a valid plan (streaming stall). Detect this with a timeout
  or token budget and push accumulated reasoning content into the next attempt
  rather than starting cold, so the retry has context on what was tried.
  Additionally, give the planner an explicit escape hatch: a structured
  `rejection` response (distinct from a plan) it can emit when the task is
  outside its capabilities — e.g. required tools are not registered, the
  request is contradictory, or it cannot construct a valid DAG. The
  orchestrator surfaces this to the user rather than retrying indefinitely.
- **Upstream context awareness** — when assembling a node's context, include the
  descriptions of its direct dependents so upstream nodes (synthesis, summary)
  know what aspect the downstream node needs. One change in the executor's
  context-assembly logic.
- **Per-tool HITL configuration** — extend HITL beyond plan approval to
  individual tool calls. Each registered tool gets a configurable approval
  policy (`auto` / `always` / `never`); the executor checks the policy before
  invoking and pauses for user confirmation when required. The ACP
  `session/request_permission` primitive is already in place. *[needs further
  spec: policy schema, default, how auto-mode decides]*
- **Summary node format variants** — add a `format` field (`concise` / `medium`
  / `verbose`) to summary nodes, routed through `_LLM_NODE_SPECS`. Lets the
  planner match compression level to downstream purpose.

Exit gate: eval scores meet an agreed bar — this is the gate that unlocks the
first PyPI publish.

## Phase 2 — Foundation Assistant

Memory, Obsidian tools, channels (chat/task/job), Telegram gateway, file editing, `foreach` node, registry output schemas.

- **File editing tools** *(first Phase 2 item — unblocks TUI coding harness)* — `file_edit(path, old_str, new_str)` with exact-once match semantics (fails closed if old_str matches zero or multiple times), `file_create` (new files only), `file_write` (full overwrite). Requires: path-root confinement (`tools.fs_roots` in Settings — currently missing), git-repo guard (configurable), risk-derived HITL defaults (write tools default to `ask`), diff-in-approval payload so humans approve a *change* not raw args. Also closes a latent gap: `_approve_tool_call` does not currently honour the `risk` field despite it being documented as governing execution. `obsidian_write` is a thin specialisation of this (vault root as `fs_roots`), not a parallel implementation. See `vault/Spec - File Editing Tools.md`.

- **Tool registry output schemas** *(ships alongside file editing)* — tools declare their output shape at `@harness.tool` registration time. The validator checks `args_template` references against the producing tool's registered schema at plan-validation time, catching tool→tool key mismatches before execution. No burden on the LLM planner — schemas are author-supplied, not LLM-generated. Retroactively add schemas to existing built-in tools. See `vault/Spec - Node Contracts and Context Flow.md`.

- **`foreach` node type** *(after upstream context awareness and registry schemas land)* — fan-out iteration over a list using the existing subplan mechanism. Fields: `foreach_source` (upstream node id or literal list), `foreach_body` (nested Plan), `foreach_aggregation` (`collect` or `synthesis`). Iterations are independent and parallelisable with a bounded concurrency semaphore. Aggregation v1: `collect` (list of outputs) and `synthesis` (hand off to a synthesis node). No `reduce` or conditional loops. Conditional loops ("repeat until X") belong at the orchestrator layer. See `vault/Spec - Foreach Node (Loops).md`.

- **HITL clarifying-questions node** — a new node type that pauses execution to
  ask the user a targeted question before proceeding, distinct from plan
  approval. The model decides what to ask; the answer is injected into
  downstream context. *[needs further spec: node schema, how answer flows into
  dependents, interaction with ACP session/request_permission vs. a new method]*
- **RAG-like planner aids** — a read-only tool the planner can invoke to pull
  pre-written plan templates or task-type guidelines from a known location
  (e.g. Obsidian vault notes). Gives the planner a starting scaffold for
  well-understood task types rather than reasoning from scratch each time.
  *[needs further spec: retrieval mechanism, file format, update workflow]*
- **Top-k planner cookbook candidates** — evolve the current single-candidate
  recipe injection into a top-k selection (`k=2–3`) so the planner can compare
  nearby plan shapes without paying for a second planning model call. This is
  the incremental step before a dedicated planner preflight/classifier node or
  external cookbook retrieval from the vault.
- **TUI: chat session mode** — evolve the TUI from a one-shot viewer into a
  persistent chat-style interface with a live input composer, making it the
  primary HITL surface. Subsumes the coding-agent harness idea. Depends on
  channels work above.
- **Token-level streaming in TUI** — surface token-by-token output in the Rust
  client as the model generates, giving a live sense of progress. Currently
  deferred in the ACP adapter.
- **Markdown rendering in the TUI output pane** — render common Markdown
  structure (headings, bullets, block quotes, code fences, emphasis, links)
  instead of displaying raw Markdown text. This should pair with the planned
  wrap-aware/freeform scrolling work so formatted output does not clip or make
  copy/selection worse.

## Phase 2.5 — Dreaming

Memory consolidation cron, audit trail, user-curation workflow.

- **Long / medium / short-term memory model** — define the three tiers (what
  qualifies, lifetime, retrieval) and how they are managed and surfaced to
  nodes. *[needs further spec: tier definitions, eviction/consolidation rules,
  injection points in context assembly]*

## Phase 3 — Insomnia

Autonomous research, scheduler, web tools, emergent subplans, mid-graph subplans, parallel sub-agents.

- **Loops in plans** — allow the planner to emit a loop construct with an
  explicit termination condition (iterate a list, retry until predicate, etc.)
  and an output-aggregation strategy. Requires non-trivial validator and
  executor changes. *[needs further spec: loop node schema, termination
  semantics, aggregation modes]*

## Phase 4 — Production & Community

Security & sandboxing, MCP client, SKILL.md plugin format, plugin SDK + marketplace, A2A, voice, K8s manifests.
