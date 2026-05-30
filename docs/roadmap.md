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

Exit gate: eval scores meet an agreed bar — this is the gate that unlocks the
first PyPI publish.

## Phase 2 — Foundation Assistant

Memory, Obsidian tools, channels (chat/task/job), Telegram gateway, reasoning mode (`/think`).

## Phase 2.5 — Dreaming

Memory consolidation cron, audit trail, user-curation workflow.

## Phase 3 — Insomnia

Autonomous research, scheduler, web tools, emergent subplans, mid-graph subplans, parallel sub-agents.

## Phase 4 — Production & Community

Security & sandboxing, MCP client, SKILL.md plugin format, plugin SDK + marketplace, A2A, voice, K8s manifests.
