# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Planner/result prompts now preserve load-bearing facts, branch labels, and
  comparison coverage using structural wording only; prompt tests guard against
  visible eval-task vocabulary, and the comparison eval regex accepts the full
  product spelling alongside the shorthand.
- Planner/result prompts now also preserve source-of-knowledge constraints,
  require deduction tasks to surface key steps in the final answer, and default
  final answers to prose unless JSON is requested. Candidate coverage regexes
  accept separator variants in product names.
- Result nodes retry once at the next configured tier after malformed output,
  branch result prompts preserve both the branch label and answer value, and
  closed-book sibling evaluation prompts repeat stable-knowledge instructions.
  The arithmetic eval accepts decimal unit formatting for equivalent weights.
- First-party file tools now support optional `[tools] fs_roots = [...]` path
  confinement. `file_read` and `image_generate` resolve paths through the shared
  guard, deny symlink/traversal escapes when roots are configured, and preserve
  the current unconfined default with a one-time warning.
- Tool approval now honors declared tool risk by default: explicit per-tool
  overrides still win, while `write` and `execute` tools resolve to `ask`
  unless overridden. Auto mode continues to treat `ask` as allowed for
  unattended runs.
- CLI coverage now protects the release-critical command surface: `version`,
  `run --json` envelope shape, `exec-plan --json` with a harness-mechanics
  fixture, and `tui` / `acp` help rendering.
- Pre-release hygiene cleanup refreshed the name-availability checker defaults
  for the current GitHub organization and documented the opt-in mflux-backed
  `image_generate` tool in the README.
- Eval-integrity cleanup removed suite-specific vocabulary from conditional
  retry detection and planner cookbook keywords, replaced it with structural
  signals, and added the scripted harness-mechanics regression wall to CI.
- Added `eval-framework/suites/glassrail-heldout`, a 12-task OpenRouter-backed
  held-out suite with an explicit no-iteration rule for release-gate
  confirmation and overfit-gap reporting.
- Engineering specs under `docs/specs/` from the June 2026 architecture audit
  — eval integrity (de-overfitting, held-out suite, CI eval gate), parallel
  node execution, node resilience, configurable routing table, security
  baseline, serving hardening, small fixes, and comparative baselines — wired
  into the docs nav and referenced from a restructured roadmap (a release
  0.1.0 blocking workstream plus Phase 2 sliced into ordered tracks).
- Per-tool approval policy is now configurable with `allow`, `ask`, and `deny`
  policies plus an `auto` execution mode that treats `ask` as `allow` while
  preserving explicit denies. ACP surfaces `ask` tool calls through
  `session/request_permission`, including an "always allow" promotion for the
  running agent process.
- Rust TUI graph view now draws routed box-drawing edges between plan nodes.
  The ACP `plan_graph` extension includes explicit data/control edges while
  keeping per-node `deps` for compatibility.
- Summary nodes now support a `format` hint (`concise`, `medium`, `verbose`).
  The executor selects concise or verbose summary prompts when requested while
  preserving the existing configurable medium/default summary prompt.
- Added a `subplan-correct` glassrail capability eval that requires a naturally
  partitioned task to include a `subplan` trajectory step.
- Streaming text events now carry node metadata: `NodeOutputChunk` includes the
  node type, and ACP `agent_message_chunk` updates include glassrail extension
  fields (`nodeId`, `nodeType`, `isFinal`) so clients can distinguish
  intermediate think/summary/synthesis output from the final result.
- Planner prompts now include a tool capability digest that groups registered
  tools by broad capability before listing the raw JSON schemas, helping the
  planner choose available tool families and reject absent capabilities.
- Planner cookbook recipes now live as bundled JSON files with descriptions,
  selection keywords, adaptable skeletons, and adaptation notes. The planner
  injects one selected recipe as a scaffold rather than hardcoded prompt text
  or a verbatim template.
- Planner cookbook selection now injects the top three ranked candidate
  recipes into the planning prompt, letting the model compare nearby DAG shapes
  without an extra planner/classifier call.
- Planner/eval guidance now tightens tool-name discipline for optional web
  tools, vague-request handling, recommendation phrasing, non-null node
  descriptions, and prose numeric answers based on the latest OpenRouter eval
  failure analysis.
- OpenRouter eval prompts now identify closed-book research/comparison tasks,
  require explicit comparison axes and prose recommendations, and call out
  planted summary facts so the model does not hide behind missing-context
  caveats or over-compress named entities.
- OpenRouter mirror eval suites now grade LLM criteria through OpenRouter
  (`anthropic/claude-haiku-4.5`) using `OPENROUTER_API_KEY`, avoiding hidden
  dependence on Claude Code subscription quota for judge calls.
- Eval criteria now separate trajectory checks from LLM answer-quality checks,
  relax wording-sensitive regexes for cache/migration references and structured
  numeric answers, and make the subplan-correct task respect the configured
  two-subplan cap.
- Summary evals now capture the installed source documents for the LLM judge,
  so faithfulness checks can compare the answer against the actual fixture
  instead of returning UNKNOWN for lack of evidence.
- Think/result node prompts now allow well-established stable knowledge when a
  task explicitly asks for it and no file, tool, or live lookup is required,
  avoiding false "missing context" failures in closed-book evals.

### Changed
- Renamed the public root exception from `DagagentError` to `GlassrailError`
  before the first PyPI release.
- Documentation corrections from the architecture audit: the README no longer
  describes the DAG viewer's layers as "parallel" (node execution is currently
  sequential; parallel execution is specced), the generation-ceiling default
  reads `20000` to match settings, and a Security notes section states the
  current posture plainly. Stale repository URLs in `docs/evals.md` and
  `docs/deployment.md` now point at the current repo, `docs/index.md` reflects
  the Phase 1 gate status, `AGENTS.md` is re-synced with `CLAUDE.md`, and
  `PHASE1_REMAINING.md` is absorbed into `docs/specs/eval-integrity.md` and
  removed.
- Planner validation now repairs missing or blank node descriptions before
  strict schema validation, including nested subplans, so otherwise-valid plans
  are not discarded for a recoverable LLM omission.
- Planner output normalization now wraps a terminal synthesis-only plan in a
  result node, and the orchestrator retries conditional-looking requests when
  the planner collapses them into a plan with no decision node.
- Planner JSON parsing now preserves an earlier non-null value when a model
  repeats the same key later as `null`, recovering otherwise-valid decision
  nodes with duplicate `condition` fields.
- Subplan execution now includes the parent task text in the nested task
  request, so closed-book subplans retain stable-knowledge instructions without
  seeing unrelated parent-node results.
- Planner cookbook and prompt guidance now steer obvious binary branches,
  logic-puzzle deductions, and comparison/recommendation tasks toward explicit
  decision, reasoning, and per-axis comparison structure.
- Planner subplan guidance now explicitly shows the correct nested tool-node
  shape (`"type": "tool", "tool": "web_search"`), contrasts it with the invalid
  `"type": "web_search"` schema, and reminds the model to count subplan nodes
  before exceeding the configured cap.
- Executor context assembly includes direct dependent-node descriptions in the
  current node's prompt, so upstream summary, synthesis, tool, decision, and
  subplan nodes can shape their output for known downstream consumers without
  seeing unrelated sibling results.
- Planner invalid-JSON failures now distinguish short parse errors from
  generation stalls using a configurable planner-budget character multiplier.
  Stall attempts preserve the raw output as `error_detail` and feed a truncated
  copy into the next retry prompt so the model does not repeat it.
- Planner rejections are logged at warning level with structured
  `rejection_reason` and best-effort `rejection_class` fields for operators.
- Planner subplan guidance now defines good boundaries, anti-patterns, schema
  expectations, and examples so nested plans are used for self-contained
  multi-step sub-tasks instead of single-node wrappers.
- TUI transcript and composer rendering now pre-wrap to the pane width before
  computing scroll offsets. Long streamed results stay fully scrollable, and
  long prompts wrap inside a composer that grows up to a small cap.
- TUI live `think` chunks render as dim italic quote-style transcript cells,
  using the ACP node metadata added for intermediate output streams.
- Tightened the default planner, decision, think, summary, synthesis, result,
  and shape-check prompts to make node roles clearer, preserve downstream
  information, and avoid over-compressed or invented outputs.
- Plan validation now enforces node-type contracts before execution: tool
  nodes must name a tool, non-tool nodes cannot carry tool fields, decision
  nodes must declare a binary yes/no branch contract, and only subplan nodes
  can carry nested plans.
- Planning retries now feed schema/validation failures back into the next
  planner attempt, so the model can repair a concrete invalid DAG instead of
  retrying blind.
- Plan validation now rejects `forced_tier` values outside the configured tier
  range, including inside nested subplans, so planner mistakes fail before
  execution.
- Planner prompt context now includes the eligible/configured tier surface and
  a concise plan cookbook (direct answer, tool→result, research, aggregation,
  conditional, subplan, rejection) so plans are shaped against the runtime the
  executor will actually use.
- TUI DAG view (`Tab`): a collapsible panel showing the plan's nodes grouped
  into dependency layers (parallel cohorts), recoloured live by status. The
  adapter sends the graph topology as a `plan_graph` extension update, since
  ACP's flat plan omits edges.
- TUI composer editing: in-place cursor movement (`←`/`→`, `Home`/`End`,
  `Backspace`/`Del`) with a visible cursor, and submitted-task history recall via
  `Ctrl-P`/`Ctrl-N`. (Multi-line entry is not yet supported.)
- TUI responsiveness: an animated spinner and a live elapsed-time readout while
  a turn runs (the turn-ended notice reports how long it took), plus
  mouse-wheel scrolling of the transcript.
- Richer TUI transcript: tool calls show their arguments and a result preview,
  and each node carries a dim tier/confidence annotation (flagged when low). The
  adapter sends tool `rawInput`/`rawOutput` and a `node_meta` extension update on
  node completion; standard ACP clients ignore the extension.
- Cancellation: a `cancelled` task status and a `TaskCancelled` terminal event.
  The orchestrator handles `asyncio.CancelledError` in run/resume/revise —
  marking the task cancelled, emitting the event, and persisting state — so an
  ACP `session/cancel` (Esc in the TUI) leaves consistent state. The adapter
  cancels the in-flight turn at a single point so cleanup is not interrupted.
- Dovetailing ACP sessions: a follow-up `session/prompt` in the same session
  carries the previous task's `final_output` forward as a context preamble, so
  tasks build on one another. Threaded as task input, leaving the
  fresh-context-per-node invariant intact.
- Rust terminal client (`clients/tui`, `glassrail-tui`): a ratatui app that
  spawns `glassrail acp`, submits tasks, streams the plan and node execution,
  and drives the plan-approval gate (approve / reject / reject-with-feedback).
  Polyglot monorepo: a dedicated `rust-tui` CI job runs fmt/clippy/build/test.
- ACP adapter (`glassrail acp`): a JSON-RPC 2.0 server over stdio exposing the
  agent via the Agent Client Protocol, for the forthcoming Rust TUI and other
  ACP clients. Implements `initialize`, `session/new`, `session/prompt`, and
  `session/cancel`, bridging the EventBus into `session/update` notifications
  (plan, tool calls, message chunks). `fs/*`, `terminal/*`, and `session/load`
  are intentionally unsupported. The adapter drives the HITL plan gate over
  `session/request_permission`: clients approve a plan or reject it with
  free-text feedback to trigger a guided replan.
- Guided replan in the engine: `Planner.plan`/`plan_attempt` accept `feedback`
  that is woven into the planning prompt, and `Orchestrator.revise(task_id,
  feedback)` re-plans a task paused at the confirmation gate and re-enters the
  gate.
- Package skeleton (src-layout) with subpackages for core, config, events,
  providers, state, harness, validator, planner, executor, channels,
  gateways, and cli.
- Core domain types: `Plan`, `Node`, `NodeStatus`, `NodeResult`,
  `BranchLogEntry`, `TaskStatus`, `ExecutionState`, ULID-based `TaskId`.
- Configuration via `pydantic-settings` with env, `.env`, and `config.toml`
  precedence; structured `TierConfig` for each tier.
- Tool harness: `@harness.tool` decorator, entry-point discovery
  (`glassrail.tools` group), and built-in tool stubs.
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
- Typer CLI entry point with a `glassrail run` command — a headless run that
  plans and executes a task in-process and prints a JSON result envelope
  (final output, normalized trajectory, status, token count) for eval harnesses
  to consume — and a `glassrail tui` command: a Rich terminal client that submits
  a task to a running gateway and renders its live SSE event stream (plan →
  per-node progress → final output), built from a thin event client and a pure,
  testable view model.
- Live DAG view in the TUI: once the plan arrives, the viewer draws it as boxes
  connected by routed edges — nodes grouped into topological layers (same layer
  = runs in parallel), edges split with pass-through vertices so they never
  cross a box — above the existing node table. Each box shows the node's
  id/type and a short summary (its planner `description`); the border is
  recoloured as the node starts, completes, or fails, and decisions show the
  branch they took. Pure render over the plan plus accumulated node statuses,
  onto a character grid that falls back to a compact list when the terminal is
  too narrow; `glassrail tui --no-dag` shows only the table.
- Shared runtime composition root (`glassrail.runtime.build_runtime`) that wires
  the harness, router, planner, validator, executor, store, and orchestrator
  from settings; the REST gateway and the CLI both build from it.
- OpenTelemetry tracing (`glassrail.telemetry`): the planner, router, and
  executor emit a span tree (task → plan / node → LLM call) with GenAI
  semantic-convention attributes (system, model, tokens) and `glassrail.*`
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
  `glassrail-cli` and `glassrail-gateway` (drive the real planner + executor over
  the agent's own tier routing), `openai-compat` (baseline a raw model), and
  `claude-cli`. Ships a `glassrail` suite (a decision-branch control pair, a
  calibration fact, and a multistep recommendation). Self-documented (its own
  README/DECISIONS/CLAUDE) and kept out of the package's ruff/pyright/pytest
  scope.
- Per-node output-token budgets (`settings.budgets`, a `NodeBudgets` table):
  each LLM call's `max_tokens` is configurable per role — planner, think,
  summary, synthesis, result, decision, extract_args, shape_check — with
  generous defaults so reasoning and summaries get room while structured
  micro-calls stay small. Override under `[budgets]` in `config.toml` or
  `GLASSRAIL_BUDGETS__<FIELD>`. Replaces the single `max_node_output_tokens`
  setting and the previously hard-coded caps in the planner and executor.
- Configurable per-node system prompts (`settings.prompts`, a `NodePrompts`
  table): the planner and executor read each role's prompt from settings
  instead of hard-coding it, so prompts can be tuned without editing source.
  Defaults live in `glassrail.config.prompts`; override under `[prompts]` in
  `config.toml` or `GLASSRAIL_PROMPTS__<FIELD>`.
- First-party tool integrations layer (`settings.tools`): bundled, opt-in tools
  configured under `[tools.*]` and registered by `build_runtime`, distinct from
  third-party entry-point plugins. First integration: **web** — `web_fetch(url)`
  fetches a page and extracts its main text via trafilatura (boilerplate
  removed), for reading and high-fidelity summarisation of webpages. Off by
  default; needs the optional `web` extra (`pip install glassrail[web]`) and
  `tools.web.fetch = true`. Adds `web_search(query)` behind a pluggable
  provider — `duckduckgo` (HTML scrape, no setup) or `searxng` (self-hosted
  JSON API); switching is a config flip (`tools.web.search`). A non-200 from
  DuckDuckGo (e.g. its HTTP 202 anti-bot challenge) is surfaced as an error
  rather than a silently empty result set. The old `web_search` built-in stub
  is removed in favour of this real implementation.
- Opt-in third-party tool plugins: with `load_tool_plugins = true`
  (`GLASSRAIL_LOAD_TOOL_PLUGINS`), `build_runtime` discovers and registers tools
  advertised through the `glassrail.tools` entry-point group. The harness has
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
