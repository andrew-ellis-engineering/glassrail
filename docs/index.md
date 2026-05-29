# dagagent

A DAG-planning agent with deterministic tier routing, fresh context per node, and plan-as-document semantics.

## Why DAG planning?

Most agents loop: think, act, observe, think again. That works, but it's hard to inspect, hard to test, and easy to lose track of what the model decided and why.

`dagagent` plans first. Every task becomes a validated directed acyclic graph of nodes. Each node sees only the inputs it declared. The graph is the plan, the plan is the audit log, and the audit log replays.

## Core invariants

- **Fresh context per node** — a node sees only the upstream outputs it declared as `context_needed`. No leaked state.
- **Plan as document** — plans are inspectable, replayable, visualizable.
- **Deterministic tier routing** — local-first, with timeout fallthrough to cloud tiers.
- **Branch by decision node** — control flow is explicit in the graph, not implicit in the model's choices.

## Status

Phase 0.5 (OSS foundation) is complete — the engine runs end to end with
SQLite persistence, the full node taxonomy, a streaming provider, and a
typed event stream. See [Roadmap](./roadmap.md) for what's landed and
what's next.
