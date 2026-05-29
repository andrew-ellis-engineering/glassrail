# Architecture

*Stub — full architecture doc to be ported from the design vault.*

## Layered view

```
┌─────────────────────────────────────────────────────────┐
│ Gateways (REST, future: TUI, Telegram, WhatsApp)        │
├─────────────────────────────────────────────────────────┤
│ Channels (chat / task / job)                            │
├─────────────────────────────────────────────────────────┤
│ Orchestrator                                            │
│   ├─ Planner (LLM call → Plan)                          │
│   ├─ PlanValidator (DAG + invariants)                   │
│   └─ Executor (topological + branch logic + HITL)       │
├─────────────────────────────────────────────────────────┤
│ Harness (tool registry, decorator, entry-point loader)  │
├─────────────────────────────────────────────────────────┤
│ Providers (LLM Protocol + TierRouter)                   │
│ State (StateStore Protocol + memory + sqlite)           │
│ Events (typed Pydantic events + in-process bus)         │
└─────────────────────────────────────────────────────────┘
```

## Node types

- `tool` — invoke a registered tool
- `decision` — binary branch on output
- `think` — reasoning-only, no tool dependency
- `summary` — N→1 context compression
- `result` — terminal output (multiple per plan allowed; one fires)
- `subplan` — terminal-only plan expansion (v1)

## Channels

| Channel | Purpose | Tools | Guardrails |
|---|---|---|---|
| chat | Conversational interface | `read_memory`, `mark_memory` | prompt-injection scanning |
| task | Full DAG flow with HITL | full toolset | tool-output validation |
| job | Static pre-written plans | scheduled-source validation |
