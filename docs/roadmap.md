# Roadmap

*Stub — full roadmap lives in the design vault and is being ported here.*

## Phase 0 — Prototype (done)

Single-file FastAPI prototype in `agent_server.py`. Validates DAG planning + tier routing + audit logging end to end.

## Phase 0.5 — OSS Foundation (in progress)

- Package restructure (src-layout, subpackages)
- State persistence (SQLite)
- Extended node types: `think`, `summary`, `result`, `subplan`
- HITL approval primitive
- Property tests for the fresh-context invariant
- Pluggable LLM provider interface (`Protocol`, streaming)
- Pluggable state store interface

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
