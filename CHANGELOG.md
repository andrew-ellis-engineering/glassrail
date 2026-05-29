# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Package skeleton (src-layout) with subpackages for core, config, events,
  providers, state, harness, validator, planner, executor, channels,
  gateways, and cli.
- Core domain types: `Plan`, `Node`, `NodeStatus`, `NodeResult`,
  `BranchLogEntry`, `TaskStatus`, `ExecutionState`, ULID-based `TaskId`.
- Configuration via `pydantic-settings` with env, `.env`, and `config.toml`
  precedence; structured `TierConfig` for each tier.
- Tool harness: `@harness.tool` decorator, entry-point discovery
  (`dagagent.tools` group), and built-in tool stubs.
- LLM provider abstraction: streaming `LLMProvider` Protocol,
  `TierRouter` with `ProviderUnavailableError`-driven fallthrough,
  OpenAI-compatible concrete provider that parses the SSE stream
  token-by-token (content deltas, tool-call accumulation, usage).
- StateStore Protocol with in-memory and SQLite (aiosqlite) backends,
  and a shared contract test suite every backend must pass.
- Plan validator: topological sort, cycle detection, tool name checks,
  decision-nesting limit, branch-reference sanity.
- Planner with JSON-mode prompt; node terminology consistent with core.
- Executor with per-node fresh context, tool / decision / synthesis
  dispatch, branch skip propagation, low-confidence flagging.
- Hypothesis property tests asserting the fresh-context invariant
  (no out-of-context content leaks into assembled node prompts).
- `think` node type for explicit reasoning steps. Defaults to tier 2
  (reasoning tier) and emits a structured `{reasoning, confidence}`
  payload.
- `summary` node type for condensing noisy upstream context. Defaults
  to tier 0 and emits a `{summary, confidence}` payload.
- `result` node type as the explicit terminal-output marker. The last
  completed `result` node's output is the task's `final_output`; plans
  without a `result` node fall back to the last completed `synthesis`
  for backward compatibility.
- `subplan` node type: a node carries its own nested `Plan` which the
  executor runs inline, bubbling the nested `final_output` up as the
  subplan node's output. Validator caps: max 2 subplans per plan, max
  12 nodes per subplan (both configurable via settings).
- Orchestrator wrapping planning, optional HITL gate, execution, and
  persistence handoffs.
- Typed event stream: Pydantic events for every plan, node, branch, and
  task transition on an in-process `EventBus`; the executor and
  orchestrator emit them, and gateways subscribe via an async iterator.
- FastAPI gateway: `/task`, `/task/{id}`, `/task/{id}/resume`,
  `/task/{id}/branch-log`, `/task/{id}/events` (SSE), `/tools`, `/health`.
- Typer CLI entry point.
- Tooling: uv, ruff, pyright strict, pytest + hypothesis, pre-commit,
  MkDocs + Material. CI on Linux + macOS for Python 3.12 + 3.13.
- Apache-2.0 license.

## [0.1.0] - Unreleased

Initial development release.
