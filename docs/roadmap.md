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

- **Eval harness** — fill in `tests/eval/` with a runnable suite that scores
  planning + execution against fixtures, behind the existing `eval` marker.
  *Done when:* `uv run pytest -m eval` produces pass/fail + a score summary,
  and CI can run it (gated/optional so external-service flakiness doesn't
  block PRs).
- **OpenTelemetry GenAI spans** ✓ — the planner, router, and executor emit a
  span tree (task → plan / node → LLM call) with GenAI semantic-convention
  attributes (model, tier, tokens) plus `dagagent.*` ones. Tracing is a no-op
  until configured; the SDK + OTLP exporter live in the optional `otel` extra.
  See [Observability](observability.md).
- **WebSocket event transport** — a second consumer of the existing
  `EventBus` alongside SSE. *Done when:* a `WS /task/{id}/events` endpoint
  streams the same typed events and closes on a terminal event; producers
  (executor/orchestrator) are unchanged.
- **Docker production image** — a real `Dockerfile` (compose is dev-only
  today). *Done when:* `docker run` serves the REST gateway from a slim,
  non-root image built in CI.
- **TUI** — a terminal client that submits a task and renders the live event
  stream. *Done when:* it shows plan → per-node progress → final output from
  a running server.

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
