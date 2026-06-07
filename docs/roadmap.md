# Roadmap

*Stub — full roadmap lives in the design vault and is being ported here.*

## Phase 0 — Prototype (done)

Single-file FastAPI prototype. Validated DAG planning + tier routing + audit logging end to end. Now superseded by the package.

## Phase 0.5 — OSS Foundation (complete)

- Package restructure (src-layout, subpackages) ✓
- Pluggable LLM provider interface (`Protocol`, streaming) ✓
- Pluggable state store interface ✓
- HITL approval primitive ✓
- State persistence (SQLite) ✓
- Extended node types: `think`, `summary`, `result`, `subplan` ✓
- Property tests for the fresh-context invariant ✓
- Typed event stream + SSE endpoint ✓

## Phase 1 — Reliability & Eval

Make the engine observable, measurable, and shippable. Suggested order:
eval harness first (it gates everything else and the PyPI publish), then
observability, then the operational surfaces.

- **Eval framework** ✓ — the standalone, stdlib-only `eval-framework/` runs each
  task *k* times against a pluggable subject backend and reports pass@k
  (capability) vs pass^k (reliability) with a deterministic → trajectory → LLM
  grading cascade, control pairs, and a promotion ratchet. The `glassrail-cli`
  backend drives the real planner + executor over the agent's own tier routing
  (your MLX model) via `glassrail run --json`. See [Evals](evals.md). The release
  gate below — promoting capability tasks to regression at an agreed pass^k bar —
  is still open.
- **OpenTelemetry GenAI spans** ✓ — the planner, router, and executor emit a
  span tree (task → plan / node → LLM call) with GenAI semantic-convention
  attributes (model, tier, tokens) plus `glassrail.*` ones. Tracing is a no-op
  until configured; the SDK + OTLP exporter live in the optional `otel` extra.
  See [Observability](observability.md).
- **WebSocket event transport** ✓ — `WS /task/{id}/events` is a second
  consumer of the existing `EventBus` alongside SSE: it streams the same typed
  events and closes on a terminal event, sharing one transport-agnostic event
  source. Producers (executor/orchestrator) are unchanged. See
  [Streaming events](streaming.md).
- **Docker production image** ✓ — a multi-stage `Dockerfile` serves the REST
  gateway from a slim (~60 MB), non-root `python:3.12-slim` image with a
  built-in health check. CI builds and smoke-tests it on every change. See
  [Deployment](deployment.md).
- **TUI** ✓ — `glassrail tui "<request>"` submits a task to a running gateway
  and renders the live SSE stream with Rich: plan → per-node progress → final
  output (a terminal snapshot if it connects after the task finished). See
  [Terminal UI](tui.md).
- **ACP adapter + Rust client** ✓ — `glassrail acp` exposes the agent over the
  Agent Client Protocol (JSON-RPC 2.0 on stdio), and the in-repo Rust
  `clients/tui` client drives it: submit a task, stream the plan and nodes,
  approve or reject-with-feedback the plan (guided replan), dovetail follow-up
  tasks, and cancel a run — all without a running gateway. Deferred to later:
  token-level streaming, session persistence/`session/load`, structural plan
  editing, and additional ACP clients (desktop/IDE).
- **Planner subplan guidance** ✓ — explicit instruction in the planner system
  prompt covering when and how to emit a `subplan` node, with examples. No code
  change; pure prompt improvement.
- **Planning failure mode detection** ✓ — stall detection (configurable
  character multiplier), accumulated reasoning fed into the retry prompt, and a
  structured `rejection` response the planner emits when the task is outside
  its capabilities. The orchestrator surfaces rejections to the user rather
  than retrying indefinitely.
- **Summary node format variants** ✓ — `format` field (`concise` / `medium` /
  `verbose`) on summary nodes, routed through `_LLM_NODE_SPECS`.

Exit gate: eval scores meet the bar defined below — this is the gate that
unlocks the first PyPI publish.

**Gate met. Phase 1 complete.**

Baseline established 2026-06-07 against Qwen3-8b (tier 0) + Qwen3.6-35b (tier
1) via OpenRouter (`suites/glassrail-openrouter`, `suites/node-capability-openrouter`):

| Suite | Result | Bar |
|---|---|---|
| glassrail-openrouter (23 tasks, 3 trials) | **19/23 full-pass (83%), 0 all-fail** | ≥ 80% full-pass, 0 all-fail |
| node-capability-openrouter (7 tasks, 3 trials) | **7/7 full-pass (100%)** | 100% |
| harness-mechanics (32 tasks, 3 trials) | **32/32 full-pass (100%)** | 100% |

Known gap at baseline: **subplan generation is the weakest surface.** All 4
partial-pass tasks fail with planner schema errors — the 35B model occasionally
uses tool names as node types inside subplans (`"type": "web_search"` instead
of `"type": "tool", "tool": "web_search"`) and exceeds the subplan count limit.
These are intermittent (each task passes 2/3 trials) and are the first tracked
prompt-improvement target in Phase 2.

Items deferred to Phase 2 (were not shipped, do not block the gate):

- **Upstream context awareness** — when assembling a node's context, include the
  descriptions of its direct dependents so upstream nodes (synthesis, summary)
  know what aspect the downstream node needs. One change in the executor's
  context-assembly logic.
- **Per-tool HITL configuration** — extend HITL beyond plan approval to
  individual tool calls. Each registered tool gets a configurable approval
  policy (`auto` / `always` / `never`); the executor checks the policy before
  invoking and pauses for user confirmation when required. The ACP
  `session/request_permission` primitive is already in place. *[needs further
  spec: policy schema, default, how auto-mode decides]*

## Phase 2 — Foundation Assistant

Memory, Obsidian tools, channels (chat/task/job), Telegram gateway, file editing, `foreach` node, registry output schemas, file viewer TUI panel.

- **Subplan node-type prompt fix** ✓ *(first tracked improvement against the Phase
  1 baseline)* — add a concrete example to the planner subplan guidance showing
  the correct inside-subplan node shape: `"type": "tool", "tool": "web_search"`
  not `"type": "web_search"`. Also reinforce the `max_subplans_per_plan` limit
  with a counter-example. Pure prompt change; expected to clear the iot/oltp
  partial-pass tasks and improve `subplan-correct` reliability. Measure by
  re-running `suites/glassrail-openrouter` and comparing against the Phase 1
  baseline.

- **Upstream context awareness** ✓ — when assembling a node's context, include the
  descriptions of its direct dependents so upstream nodes (synthesis, summary)
  know what aspect the downstream node needs. One change in the executor's
  context-assembly logic. *(deferred from Phase 1)*

- **Per-tool HITL configuration** — extend HITL beyond plan approval to
  individual tool calls. Each registered tool gets a configurable approval
  policy; the executor checks it before invoking and pauses for confirmation
  when required. *(deferred from Phase 1, needs further spec)*

- **File editing tools** *(unblocks TUI coding harness)* — `file_edit(path, old_str, new_str)` with exact-once match semantics (fails closed if old_str matches zero or multiple times), `file_create` (new files only), `file_write` (full overwrite). Requires: path-root confinement (`tools.fs_roots` in Settings — currently missing), git-repo guard (configurable), risk-derived HITL defaults (write tools default to `ask`), diff-in-approval payload so humans approve a *change* not raw args. Also closes a latent gap: `_approve_tool_call` does not currently honour the `risk` field despite it being documented as governing execution. `obsidian_write` is a thin specialisation of this (vault root as `fs_roots`), not a parallel implementation. See `vault/Spec - File Editing Tools.md`.

- **Tool registry output schemas** *(ships alongside file editing)* — tools declare their output shape at `@harness.tool` registration time. The validator checks `args_template` references against the producing tool's registered schema at plan-validation time, catching tool→tool key mismatches before execution. No burden on the LLM planner — schemas are author-supplied, not LLM-generated. Retroactively add schemas to existing built-in tools. See `vault/Spec - Node Contracts and Context Flow.md`.

- **`foreach` node type** *(after upstream context awareness and registry schemas land)* — fan-out iteration over a list using the existing subplan mechanism. Fields: `foreach_source` (upstream node id or literal list), `foreach_body` (nested Plan), `foreach_aggregation` (`collect` or `synthesis`). Iterations are independent and parallelisable with a bounded concurrency semaphore. Aggregation v1: `collect` (list of outputs) and `synthesis` (hand off to a synthesis node). No `reduce` or conditional loops. Conditional loops ("repeat until X") belong at the orchestrator layer. See `vault/Spec - Foreach Node (Loops).md`.

- **HITL clarifying-questions node** — a new node type that pauses execution to
  ask the user a targeted question before proceeding, distinct from plan
  approval. The model decides what to ask; the answer is injected into
  downstream context. *[needs further spec: node schema, how answer flows into
  dependents, interaction with ACP session/request_permission vs. a new method]*
- **RAG-like planner aids** — a read-only tool the planner can invoke to pull
  pre-written plan templates or task-type guidelines from a known location
  (e.g. Obsidian vault notes). Gives the planner a starting scaffold for
  well-understood task types rather than reasoning from scratch each time.
  *[needs further spec: retrieval mechanism, file format, update workflow]*
- **Top-k planner cookbook candidates** ✓ — evolve the current single-candidate
  recipe injection into a top-k selection (`k=2–3`) so the planner can compare
  nearby plan shapes without paying for a second planning model call. This is
  the incremental step before a dedicated planner preflight/classifier node or
  external cookbook retrieval from the vault.
- **TUI: chat session mode** — evolve the TUI from a one-shot viewer into a
  persistent chat-style interface with a live input composer, making it the
  primary HITL surface. Subsumes the coding-agent harness idea. Depends on
  channels work above.
- **Token-level streaming in TUI** — surface token-by-token output in the Rust
  client as the model generates, giving a live sense of progress. Currently
  deferred in the ACP adapter.
- **Markdown rendering in the TUI output pane** — render common Markdown
  structure (headings, bullets, block quotes, code fences, emphasis, links)
  instead of displaying raw Markdown text. This should pair with the planned
  wrap-aware/freeform scrolling work so formatted output does not clip or make
  copy/selection worse.
- **TUI mouse support and tab navigation** — the TUI should be fully mouse-aware:
  clicking on text input focuses it, clicking on output selects/highlights it,
  and the top-level chrome uses a tab bar so distinct surfaces (chat, DAG view,
  and future panels) are navigable by click as well as keyboard shortcut. The
  tab bar is the load-bearing primitive here — it gates the ability to add new
  first-class surfaces (task history, tool trace, memory inspector, settings)
  without the layout becoming a keyboard-only maze. Design constraints: tabs
  must be keyboard-accessible too (arrow keys or number shortcuts) so the TUI
  stays usable over SSH; mouse support should degrade gracefully in terminals
  that don't report mouse events. Applies primarily to the Rust TUI
  (`clients/tui` / ratatui); the Python Rich TUI is a simpler read-only viewer
  and a lower priority for mouse work.
- **TUI copy/paste support** — the TUI currently captures raw keyboard input,
  blocking OS clipboard shortcuts (`Cmd+C`/`Cmd+V`) and making it impossible to
  copy agent output without terminal-emulator-specific workarounds (e.g.
  Option-drag in iTerm2). Fix on two axes: (1) selecting text in the output
  pane should write to the system clipboard automatically, and (2) a dedicated
  keybind (e.g. `y` or `Cmd+C` when output is focused) should copy the last
  complete node output or the full final result. Should be implemented alongside
  the Markdown rendering and wrap-aware scrolling work, since all three affect
  how output text is laid out and selectable. Applies to both the Rust TUI
  (`clients/tui`) and the Python Rich TUI (`glassrail tui`).
- **TUI: file viewer panel** *(ships with file editing tools — they are a unit)*
  — a dedicated tab in the Rust TUI for browsing the local file tree and
  viewing file contents. Primary role is surfacing diffs when the agent proposes
  an edit: instead of approving raw tool args through the HITL gate, the user
  sees an inline diff of the proposed change and approves or rejects it with
  full context. Secondary role: general file inspection without leaving the TUI.
  Requires: tab bar + mouse support (above), file editing tools, and the
  diff-in-approval payload those tools produce. Sequence constraint — do not
  ship the file editing tools without this panel; approving blind edits is a
  worse UX than the current no-edit state.

## Phase 2.5 — Dreaming

Memory consolidation cron, audit trail, user-curation workflow, cloud tier routing, observability TUI panel.

- **TUI: observability panel** *(gates on: tab bar + mouse from Phase 2)* — a
  dedicated tab surfacing the run telemetry that already exists but has no
  visual home: OTel span tree (task → plan → node → LLM call), per-node token
  counts and latency, tier routing decisions (which tier was used, whether
  fallthrough occurred), and confidence scores per node. All data flows over the
  existing typed event stream and OTel; this is a rendering job, not new
  plumbing. Pairs naturally with the audit trail work also in this phase.
  Read-only; no interaction required beyond navigation. Primary surface: Rust
  TUI.

- **Long / medium / short-term memory model** — define the three tiers (what
  qualifies, lifetime, retrieval) and how they are managed and surfaced to
  nodes. *[needs further spec: tier definitions, eviction/consolidation rules,
  injection points in context assembly]*

- **`glassrail routing recompute` — one-shot tier-ROI model selector** *(prerequisite:
  cloud tiers 2–3 wired to real OpenRouter endpoints)* — a CLI command that
  deterministically selects the highest-ROI OpenRouter model for each cloud tier
  (2–3; local tiers 0–1 are out of scope) and writes `routing_table.json` for
  the tier router to consume. Not a cron — run manually and inspect outputs for
  several weeks before trusting automation.

  **Algorithm:** for each candidate `(model_id, provider_id, mode)` in the
  eligible pool, score `ROI = Q^α / C_eff^β` where `Q` is a quality index from
  a version-controlled `quality_scores.yaml` (maintained by hand on a
  weekly–biweekly cadence; not auto-pulled) and `C_eff = (w_in·price_in +
  w_out·price_out) · (1 + credit_fee)` using workload blend weights from
  telemetry (default `3:1` input:output). Default `α = β = 1` → cost-leaning:
  the quality band already enforces the floor, so cheapest-in-band wins unless
  `α` is raised.

  **Eligibility filters before scoring:** in-band quality index, `status ==
  "current"` (no deprecated/preview unless opted in), required modalities,
  minimum context length, effective-cost ceiling per tier, minimum provider
  count (resilience), provider allow/blocklist (data-residency policy), and the
  promo-price rule (score on list price unless the promo outlives the next
  scheduled recompute).

  **Tiebreak + hysteresis:** total-order sort `(-roi, -quality, cost_eff,
  model_id, provider_id, mode)` guarantees a unique winner. Incumbent stays
  selected unless the challenger's ROI beats it by ≥ 5% (`hysteresis`
  threshold) — prevents day-to-day flapping from minor price moves.

  **Safety invariant:** after writing `routing_table.json`, the validator must
  assert that the selected models maintain the router's monotonicity assumption
  (T2 quality < T3 quality < T4 quality). ROI optimisation within a band can
  silently invert capability ordering if the assertion is absent.

  **Outputs:** `routing_table.json` (atomic rename publish) + append-only
  `selection_log.jsonl` recording winner, runner-up, both ROI scores, decision
  reason (`selected_challenger` / `unchanged` / `kept_incumbent__within_hysteresis`
  / `kept_prior__empty_pool`), `snapshot_hash`, and `config_hash` per tier per
  run — answers "why did Tier 3 change?" months later.

  **Open questions to resolve at build time:** (1) confirm `α=β=1` cost-leaning
  default or raise `α` to bias toward band-top quality; (2) programmatic
  Artificial Analysis access for `Q` or manual `quality_scores.yaml`; (3)
  measured input:output blend weights from telemetry; (4) provider blocklist
  policy (relevant given Chinese-origin models dominating value tiers); (5)
  whether to include `preview` models. Full spec: `vault/Spec - Tier ROI Model
  Selector.md`.

## Phase 3 — Insomnia

Autonomous research, scheduler, web tools, emergent subplans, mid-graph subplans, parallel sub-agents, multi-chat TUI panel.

- **TUI: multi-chat panel** *(gates on: chat session mode from Phase 2 + session
  multiplexing from Phase 3 parallelism work)* — evolve the TUI from a
  single-task workspace into a multi-task one. A session list pane shows all
  active and recent tasks with live status indicators; switching between them
  swaps the chat and DAG views to the selected session. New tasks can be
  submitted without waiting for the current one to finish. Deliberately deferred
  to Phase 3 because building it earlier would require session multiplexing
  infrastructure twice — Phase 3's scheduler and parallel sub-agent work builds
  that substrate; the multi-chat panel consumes it. Sequence: do not attempt
  this before the runtime can hold concurrent sessions.

- **Loops in plans** — allow the planner to emit a loop construct with an
  explicit termination condition (iterate a list, retry until predicate, etc.)
  and an output-aggregation strategy. Requires non-trivial validator and
  executor changes. *[needs further spec: loop node schema, termination
  semantics, aggregation modes]*

## Phase 4 — Production & Community

Security & sandboxing, MCP client, SKILL.md plugin format, plugin SDK + marketplace, A2A, voice, K8s manifests, automated tier-ROI cron, config panel, setup wizard.

- **TUI: config options panel** *(gates on: stable config schema, tier health
  reporting; sequence after config panel before setup wizard)* — a live view
  and edit surface for the active settings: tier status (healthy/degraded, which
  model is loaded, last latency), tool enable/disable toggles, fast mode toggle,
  token budgets. Writes back to `~/.glassrail/config.toml` on save. Kept in
  Phase 4 because it implies mutable runtime config — a production concern that
  overlaps with the security and plugin work. A read-only tier-status strip
  could appear earlier, but the editable panel waits for the config surface to
  stabilise through Phases 2–3.

- **TUI: setup wizard panel** *(gates on: config panel primitives; build after
  config panel, not before)* — a guided first-run flow: detect running local MLX
  servers, walk through API key entry for OpenRouter tiers, configure provider
  endpoints, run a smoke test against each tier, and write `~/.glassrail/config.toml`.
  Replaces manual TOML editing for new users. Belongs in Phase 4 because it is
  an onboarding/community surface — it only pays off once the config schema has
  stopped moving and you are optimising for first-impressions. Reuses the config
  panel's write primitives rather than re-implementing them.

- **Automated nightly tier-ROI cron** *(builds on the Phase 2.5 `routing recompute`
  CLI; requires several weeks of manual operation to establish confidence)* —
  promote the one-shot recompute command to a scheduled nightly cron
  (`17 3 * * *` UTC, off-peak). Add the production guardrails deferred from the
  Phase 2.5 CLI: price-spike detection (veto promotion if a selected model's
  `C_eff` moved > 50% vs the prior snapshot — likely a promo expiry or data
  error), pool-collapse warnings, single-flight lock (prevent concurrent runs),
  and structured alert routing (info on tier change, warn on pool collapse or
  smoke-test veto, error on fetch failure or invalid config). Optional liveness
  gate: post-selection smoke request per newly-selected model — veto the
  promotion if it errors or times out; keep the smoke result in the run log so
  the selection remains explainable. Note the trade-off: enabling the smoke test
  introduces a liveness dependency and means the promotion is no longer
  reproducible from data alone.

  **Additional controls:** per-run change budget (cap how many tiers may change
  in a single run, e.g. `change_budget_per_run: 2`), per-tier cooldown (a tier
  may not change more than once per N days), `--snapshot <path>` replay flag for
  audit and debugging against old snapshots without re-fetching. Exit codes
  wired to the health monitor: `0` success, `2` published with warnings, `3` no
  publish / kept prior, `4` invalid config. Full spec: `vault/Spec - Tier ROI
  Model Selector.md`.
