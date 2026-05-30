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
  `/task/{id}/branch-log`, `/task/{id}/events` (SSE and WebSocket — the
  WebSocket streams the same typed events and closes on a terminal event),
  `/tools`, `/health`.
- Typer CLI entry point with a `dagagent run` command — a headless run that
  plans and executes a task in-process and prints a JSON result envelope
  (final output, normalized trajectory, status, token count) for eval harnesses
  to consume — and a `dagagent tui` command: a Rich terminal client that submits
  a task to a running gateway and renders its live SSE event stream (plan →
  per-node progress → final output), built from a thin event client and a pure,
  testable view model.
- Shared runtime composition root (`dagagent.runtime.build_runtime`) that wires
  the harness, router, planner, validator, executor, store, and orchestrator
  from settings; the REST gateway and the CLI both build from it.
- OpenTelemetry tracing (`dagagent.telemetry`): the planner, router, and
  executor emit a span tree (task → plan / node → LLM call) with GenAI
  semantic-convention attributes (system, model, tokens) and `dagagent.*`
  attributes (tier, node type/status, task status). Tracing is a no-op until
  configured via settings; the SDK and OTLP/HTTP exporter ship in the optional
  `otel` extra. The REST gateway configures it at startup.
- Production `Dockerfile`: multi-stage uv build serving the REST gateway from
  a slim, non-root `python:3.12-slim` image (~60 MB) with a built-in health
  check. CI builds and smoke-tests the image on every change.
- Vendored `eval-framework/`: a self-contained, stdlib-only harness that runs
  each task k times against a pluggable subject backend, captures output /
  side-effects / trajectory, grades with a deterministic→trajectory→LLM cascade
  (the judge decoupled from the subject), and reports pass@k vs pass^k. Backends:
  `dagagent-cli` and `dagagent-gateway` (drive the real planner + executor over
  the agent's own tier routing), `openai-compat` (baseline a raw model), and
  `claude-cli`. Ships a `dagagent` suite (a decision-branch control pair, a
  calibration fact, and a multistep recommendation). Self-documented (its own
  README/DECISIONS/CLAUDE) and kept out of the package's ruff/pyright/pytest
  scope.
- Per-node output-token budgets (`settings.budgets`, a `NodeBudgets` table):
  each LLM call's `max_tokens` is configurable per role — planner, think,
  summary, synthesis, result, decision, extract_args, shape_check — with
  generous defaults so reasoning and summaries get room while structured
  micro-calls stay small. Override under `[budgets]` in `config.toml` or
  `DAGAGENT_BUDGETS__<FIELD>`. Replaces the single `max_node_output_tokens`
  setting and the previously hard-coded caps in the planner and executor.
- Configurable per-node system prompts (`settings.prompts`, a `NodePrompts`
  table): the planner and executor read each role's prompt from settings
  instead of hard-coding it, so prompts can be tuned without editing source.
  Defaults live in `dagagent.config.prompts`; override under `[prompts]` in
  `config.toml` or `DAGAGENT_PROMPTS__<FIELD>`.
- First-party tool integrations layer (`settings.tools`): bundled, opt-in tools
  configured under `[tools.*]` and registered by `build_runtime`, distinct from
  third-party entry-point plugins. First integration: **web** — `web_fetch(url)`
  fetches a page and extracts its main text via trafilatura (boilerplate
  removed), for reading and high-fidelity summarisation of webpages. Off by
  default; needs the optional `web` extra (`pip install dagagent[web]`) and
  `tools.web.fetch = true`. Adds `web_search(query)` behind a pluggable
  provider — `duckduckgo` (HTML scrape, no setup) or `searxng` (self-hosted
  JSON API); switching is a config flip (`tools.web.search`). A non-200 from
  DuckDuckGo (e.g. its HTTP 202 anti-bot challenge) is surfaced as an error
  rather than a silently empty result set. The old `web_search` built-in stub
  is removed in favour of this real implementation.
- Opt-in third-party tool plugins: with `load_tool_plugins = true`
  (`DAGAGENT_LOAD_TOOL_PLUGINS`), `build_runtime` discovers and registers tools
  advertised through the `dagagent.tools` entry-point group. The harness has
  supported entry-point discovery all along; the composition root now invokes
  it. Off by default — loading whatever is installed is a deliberate choice.
- Tooling: uv, ruff, pyright strict, pytest + hypothesis, pre-commit,
  MkDocs + Material. CI on Linux + macOS for Python 3.12 + 3.13.
- Apache-2.0 license.

### Changed
- Planner now states the structural budget (max plan nodes, subplan count and
  size) to the model in each request, derived from settings rather than
  hard-coded in the prompt. Previously the top-level node cap was never
  communicated, so the model would overshoot it and the plan would be rejected
  at validation. Raised the default `max_plan_nodes` from 12 to 24 to fit
  real fan-out tasks (an "N things × M aspects" research sweep needs N×M tool
  nodes plus aggregation).

## [0.1.0] - Unreleased

Initial development release.
