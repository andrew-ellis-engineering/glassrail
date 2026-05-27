# dagagent

> *Working name — to be replaced. See `Name Ideas.md` in the design vault for candidates.*

A DAG-planning agent with deterministic tier routing, fresh context per node, and plan-as-document semantics.

## Status

**Phase 0.5 — OSS Foundation.** Restructuring the single-file prototype (`agent_server.py`) into a multi-package, src-layout codebase with state persistence, extended node types, and pluggable LLM/state interfaces.

The prototype works end-to-end; the package skeleton in `src/dagagent/` is being filled in module by module.

## Design

- **DAG planning** — every task becomes a validated graph of nodes (no ReAct loops).
- **Fresh context per node** — each node sees only the upstream outputs it declared as `context_needed`.
- **Plan as document** — plans are inspectable, replayable, and visualizable.
- **Tiered model routing** — deterministic fallthrough from local → cloud, with timeouts.
- **Channels** — chat / task / job, each with its own toolset and guardrails.
- **Extended node types** — `tool_call`, `decision`, `think`, `summary`, `result`, `subplan`.

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) for dependency management

## Development

```bash
uv sync --all-extras --group dev
uv run pre-commit install
uv run pytest
```

## Layout

```
src/dagagent/
├── core/         Plan, Node, NodeStatus, TaskId, errors
├── config/       pydantic-settings
├── events/       typed Pydantic events + EventBus
├── providers/    LLMProvider protocol, TierRouter, concrete impls
├── state/        StateStore protocol + memory + sqlite
├── harness/      ToolHarness, decorator, entry-point loader
├── validator/    PlanValidator + invariants
├── planner/      plan generation + replan
├── executor/     topological execution, branch logic, HITL
├── channels/     chat / task / job logic
├── gateways/     rest (later: telegram, tui)
└── cli/          Typer entry point
```

## License

Apache-2.0. See [LICENSE](./LICENSE).
