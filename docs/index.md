# Glassrail

A DAG-planning agent with deterministic tier routing, fresh context per node, and plan-as-document semantics.

## Why DAG planning?

Most agents loop: think, act, observe, think again. That works, but it's hard to inspect, hard to test, and easy to lose track of what the model decided and why.

`glassrail` plans first. Every task becomes a validated directed acyclic graph of nodes. Each node sees only the inputs it declared. The graph is the plan, the plan is the audit log, and the audit log replays.

## Core invariants

- **Fresh context per node** — a node sees only the upstream outputs it declared as `context_needed`. No leaked state.
- **Plan as document** — plans are inspectable, replayable, visualizable.
- **Deterministic tier routing** — local-first, falling through to cloud tiers when a tier is unavailable (errors or times out before producing output).
- **Branch by decision node** — control flow is explicit in the graph, not implicit in the model's choices.

## Status

The Phase 1 eval gate is met (see the gate tables and integrity caveats in
the [Roadmap](./roadmap.md)) and Glassrail 0.1.0 is published on PyPI.
The engine runs end to end — plan → validate → execute over tier routing —
with SQLite persistence, the full node taxonomy, a typed event stream,
REST and ACP gateways, and the eval framework measuring it. APIs are
unstable while Glassrail is 0.x.

For the product overview, see the
[Glassrail website](https://andrew-ellis-engineering.github.io/glassrail.github.io/).
