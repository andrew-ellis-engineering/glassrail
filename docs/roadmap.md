# Roadmap

*Stub — full roadmap lives in the design vault and is being ported here.*

## Phase 0 — Prototype (done)

Single-file FastAPI prototype. Validated DAG planning + tier routing + audit logging end to end. Now superseded by the package.

## Phase 0.5 — OSS Foundation (in progress)

- Package restructure (src-layout, subpackages) ✓
- Pluggable LLM provider interface (`Protocol`, streaming) ✓
- Pluggable state store interface ✓
- HITL approval primitive ✓
- State persistence (SQLite)
- Extended node types: `think`, `summary`, `result`, `subplan`
- Property tests for the fresh-context invariant

## Phase 1 — Reliability & Eval

Docker production image, eval harness, OpenTelemetry GenAI spans, event stream (SSE/WebSocket), TUI.

## Phase 2 — Foundation Assistant

Memory, Obsidian tools, channels (chat/task/job), Telegram gateway, reasoning mode (`/think`).

## Phase 2.5 — Dreaming

Memory consolidation cron, audit trail, user-curation workflow.

## Phase 3 — Insomnia

Autonomous research, scheduler, web tools, emergent subplans, mid-graph subplans, parallel sub-agents.

## Phase 4 — Production & Community

Security & sandboxing, MCP client, SKILL.md plugin format, plugin SDK + marketplace, A2A, voice, K8s manifests.
