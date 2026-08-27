# Architecture

Glassrail is a local-first agent runtime built around validated DAG plans,
deterministic model routing, fresh context per node, and durable execution
state. This document describes the architecture that exists in the repository
today. Planned components are labeled explicitly and belong in the
[roadmap](roadmap.md).

## Design principles

1. **Models plan; code governs execution.** The planner proposes a graph.
   Validation, topological ordering, tier selection, retries, approval policy,
   branch propagation, and persistence are deterministic code.
2. **The plan is an auditable document.** A task's accepted plan, node results,
   branch log, planning attempts, status, and final output live in
   `ExecutionState`.
3. **Context is explicit.** A node receives only its own instructions and the
   outputs named by `context_needed`. This fresh-context invariant has property
   coverage.
4. **Providers are leaves.** A provider serves one model. `TierRouter` owns
   ordered fallthrough, timeout boundaries, and tier observability.
5. **Plans remain acyclic.** Iteration belongs inside a bounded node or across
   successive plans at the orchestrator layer, never in the graph topology.
6. **Safety is part of the execution contract.** Tool risk, approval policy,
   path confinement, validation, and event history are first-class runtime
   behavior.

## Layered view

```
┌─────────────────────────────────────────────────────────────┐
│ Clients and gateways                                        │
│ CLI · REST/SSE/WebSocket · ACP · Python TUI · Rust TUI      │
├─────────────────────────────────────────────────────────────┤
│ Orchestrator                                                │
│ Planning attempts · validation · HITL gate · resume/revise  │
├──────────────────────┬──────────────────────────────────────┤
│ Planner              │ Executor                             │
│ prompt + cookbook    │ ready-set scheduler · branch logic  │
│ → validated Plan     │ node retries · tool approval        │
├──────────────────────┴──────────────────────────────────────┤
│ Runtime services                                            │
│ ToolHarness · TierRouter · StateStore · EventBus · OTel     │
├─────────────────────────────────────────────────────────────┤
│ Implementations                                             │
│ OpenAI-compatible LLM · scripted LLM · memory/sqlite state  │
│ built-in/plugin tools · in-process typed events             │
└─────────────────────────────────────────────────────────────┘
```

`glassrail.core` contains the dependency-light domain types and errors. It
imports no other Glassrail package. Higher layers depend inward on those types.

## Task lifecycle

1. A gateway or CLI creates and saves an `ExecutionState`.
2. `Orchestrator.run` asks `Planner` for a plan. Failed, rejected, and
   retried attempts remain attached to the state.
3. `PlanValidator` enforces node contracts, tool existence, dependency
   integrity, acyclicity, branch references, plan limits, subplan limits, and
   configured tier bounds.
4. When plan confirmation is enabled, the task enters
   `AWAITING_CONFIRMATION`. Resume uses atomic state transitions so only one
   worker can claim execution.
5. `Executor` schedules ready nodes up to `max_concurrent_nodes`. Dependency
   edges and branch decisions determine readiness; independent nodes may run in
   parallel.
6. Node results and typed events are recorded as execution progresses. A
   terminal result becomes `final_output`; failures remain inspectable.
7. State is persisted through the configured `StateStore`.

## Plan and node model

The current node types are:

| Type | Contract |
|---|---|
| `tool` | Invoke a registered tool with static or context-derived arguments. |
| `decision` | Select one binary branch and skip the untaken branch transitively. |
| `think` | Perform explicit intermediate reasoning. |
| `summary` | Compress upstream material with a concise, medium, or verbose format hint. |
| `synthesis` | Combine multiple upstream outputs into an intermediate result. |
| `result` | Produce user-visible final output. |
| `subplan` | Execute a predeclared nested plan and bubble its final output to the parent. |

Every accepted plan receives a deterministic topological order. Decision branch
edges participate in that order. Nested subplans are independently validated and
execute with isolated internal task identifiers while publishing parent-scoped
nested event paths.

Runtime fan-out is not implemented. Its aggregation and observability contracts
remain unresolved, so it is listed as planned work rather than current
architecture.

## Execution and context

The executor uses a bounded ready-set scheduler. It dispatches nodes whose
declared dependencies are resolved, records branch skips immediately, and
preserves shared joins that still have at least one completed content input.

For each LLM node, context assembly includes only declared upstream results.
Skipped, empty, and failed inputs become explicit notices rather than hidden
absence. Direct downstream descriptions are supplied to upstream nodes so they
can preserve information their consumers will need.

Static planner and executor prefixes carry provider-neutral cache hints.
OpenRouter providers serialize explicit cache breakpoints; local
OpenAI-compatible endpoints receive unchanged string messages.

## Model providers and routing

`LLMProvider` is a streaming protocol returning `Chunk` values.
`OpenAICompatProvider` supports local servers and cloud OpenAI-compatible
endpoints. `ScriptedProvider` provides deterministic test and eval behavior.

`TierRouter` walks providers in tier order. It may fall through only before
output is emitted; a mid-stream failure belongs to the selected provider call
and is handled by the node retry policy. Node type and
`reasoning_required` choose the minimum tier through the configurable routing
table. Providers do not know about other tiers.

## Tools and approval

`ToolHarness` registers callable tools with JSON Schema input metadata and a
risk level. First-party tools use the decorator API; third-party packages can
register through the `glassrail.tools` entry-point group.

Explicit per-tool approval configuration wins over risk-derived defaults.
Read/network tools default to allow; write/execute tools default to ask.
Interactive ACP clients receive permission requests before guarded calls.

Filesystem reads can be confined to configured roots through the shared path
guard. General write tools are planned but intentionally not implemented until
their diff-preview and TUI review contracts are complete.

## State and concurrency

`StateStore` defines durable task operations. Implementations:

- `InMemoryStateStore` for tests and ephemeral runs.
- `SqliteStateStore` for local durable operation.

Both pass the shared contract suite. Atomic conditional status transitions
protect resume claims across concurrent SQLite connections. This is task-state
coordination, not a distributed node scheduler; replacing the in-process event
bus or coordinating execution across multiple workers would require additional
infrastructure.

## Events, gateways, and clients

The typed `EventBus` publishes plan, task, node, branch, approval, and output
events. Subscribers can scope by task. Slow subscribers expose dropped-event
counts rather than failing producers.

- REST submits and inspects tasks.
- SSE and WebSocket expose the same typed task event stream.
- ACP maps Glassrail plans and events to JSON-RPC for agent clients.
- The Python TUI is a read-only gateway viewer.
- The Rust TUI spawns `glassrail acp` and provides the richer interactive
  approval surface.

Nested node events carry `node_path` on REST transports. Clients that do not
yet render nested graphs filter them deliberately.

## Observability

OpenTelemetry spans form a task → plan/node → LLM-call tree. Model, tier,
tokens, retries, confidence, status, and provider-reported prompt-cache usage
are attached where available. Instrumentation is a no-op until an SDK exporter
is configured. See [observability](observability.md).

## Planned boundaries

The following are roadmap targets, not current architecture:

- Tool output schemas and constrained tool-to-tool references.
- File editing and diff-aware approval.
- `foreach` runtime fan-out.
- Persistent chat/task/job channel abstractions.
- Long-, medium-, and short-term memory.
- Scheduler-driven autonomous work.
- Tier-ROI selection and later automated recomputation.
- MCP/A2A/plugin marketplace production surfaces.

Internal engineering specifications own implementation contracts for planned
work. The public roadmap owns high-level sequencing.
