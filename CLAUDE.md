# Working in this repo

Always keep this file in sync with AGENTS.md if you make changes

`glassrail` is a DAG-planning agent: every task becomes a validated graph of
nodes, each node runs with fresh context, and tier routing is deterministic.
This file is the operating manual for working in the repo. For *what's built*
and *what's next*, see `CHANGELOG.md` and `docs/roadmap.md` — don't duplicate
them here.

**Keep `README.md` current.** When a change alters how someone *runs or
configures* the agent — a new/renamed CLI command or flag, a changed default,
a new config key, a new way to invoke it — update the README's Quickstart /
Ways to run it / Configuration sections in the same change. The README is the
user-facing front door (what it is, how to run it, where to go next); it must
not drift from the actual surface. It is **not** the changelog, the
architecture doc, or the contributor manual — link out to those rather than
inlining their content.

## Check sweep — must be green before every commit

Run all four. The bar is zero failures, zero lint findings, and a clean
pyright (no errors **and** no warnings — we hold the warning count at zero,
not just the error count).

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

CI (`.github/workflows/ci.yml`) runs the same checks on Linux + macOS across
Python 3.12 and 3.13. `pre-commit` runs ruff + pyright locally; install it
once with `uv run pre-commit install`.

## Conventions (locked — don't relitigate without raising it first)

- **Layout:** src-layout (`src/glassrail/...`), one repo, subpackages.
- **Python:** 3.12+ floor. **Async:** stdlib `asyncio` only (no anyio).
- **Deps:** `uv` with `uv.lock`. **Lint/format:** `ruff`. **Types:** `pyright`
  strict. Add a `# noqa`/rule-disable only with a reason, and prefer
  restructuring the code over suppressing.
- **IDs:** ULID. **Config:** `pydantic-settings` (env + `.env` + `config.toml`).
  **CLI:** Typer. **Logging:** stdlib `logging` + `structlog`.
- **License:** Apache-2.0. **Versioning:** SemVer 0.x. **No PyPI publish** until
  after the Phase 1 eval gates — GitHub releases only.
- **Docs:** MkDocs + Material (`mkdocs.yml`); the `nav` there is the source of
  truth for the published site, so add new pages to it. **Observability:**
  structured logs plus OpenTelemetry tracing (`glassrail.telemetry`) — a no-op
  until configured; SDK + OTLP exporter in the optional `otel` extra. See
  `docs/observability.md`.
- **No eval-task vocabulary in engine code or prompts.** Orchestrator and
  planner heuristics, cookbook `keywords`, and default prompts must use
  structural, task-shape language — never domain nouns or phrasings copied
  from eval-framework task prompts. (Tests may quote eval phrasings as
  regression inputs; engine code and prompts may not.) The held-out suite
  (`eval-framework/suites/glassrail-heldout/`, once landed) is never iterated
  against. See `docs/specs/eval-integrity.md`.

## Documentation map — which file owns what

Keep these single-purpose; put new content in the file that owns the topic
rather than duplicating across files.

- `README.md` — user-facing front door: what it is, Quickstart, ways to run it,
  configuration basics, links out. Keep current (see the rule above).
- `CLAUDE.md` (this file) — operating manual for working in the repo:
  conventions, primitives, package map, test layout, commit rules.
- `CHANGELOG.md` — the running inventory of what has landed. The README's Status
  section points here instead of listing components.
- `CONTRIBUTING.md` — the full check sweep and PR guidelines for contributors.
- `docs/index.md` — published-site landing page (the "why DAG planning?" intro).
- `docs/architecture.md` — the layered architecture and how the pieces fit
  (currently a stub being ported from the design vault).
- `docs/roadmap.md` — phases and what's next.
- `docs/specs/` — implementable engineering specs from the June 2026
  architecture audit; `docs/specs/index.md` is the index with priorities,
  sequencing, and instructions for implementing agents. Specs own the *how*,
  the roadmap owns the *when*. Flip a spec's `Status:` line (and the index
  table) when you implement one.
- `docs/evals.md` — the eval framework: pass@k vs pass^k, grading cascade, how
  to run it. (Framework internals live in `eval-framework/CLAUDE.md`.)
- `docs/streaming.md` — the task event stream (SSE and WebSocket transports).
- `docs/observability.md` — OpenTelemetry span tree and how to enable tracing.
- `docs/deployment.md` — the production `Dockerfile` and how to serve the gateway.
- `docs/tui.md` — the `glassrail tui` live viewer (read-only SSE client).

## Architectural primitives

- **LLM providers** implement a `typing.Protocol` with one streaming method,
  `async def complete(...) -> AsyncIterator[Chunk]`. Providers are "dumb": one
  model, no fallback knowledge.
- **Tier routing** lives in a separate `TierRouter` that wraps an ordered
  provider list and owns timeout/fallthrough (on `ProviderUnavailableError`,
  before any chunk is emitted).
- **State** is a single `StateStore` Protocol; every backend passes the shared
  contract suite in `tests/contract/`. Backends: `memory`, `sqlite`.
- **Events** are typed Pydantic models on an in-process `EventBus`; consumers
  subscribe via an async iterator. Built to swap for Redis/NATS without
  touching producers.
- **Tools** register via the `@harness.tool` decorator (first-party) and the
  `glassrail.tools` entry-point group (third-party).
- **Node types:** `tool`, `decision`, `synthesis`, `think`, `summary`,
  `result`, `subplan`. The four single-LLM-call types (synthesis/think/summary/
  result) share `Executor._execute_llm_node` via `_LLM_NODE_SPECS` — add a new
  one by extending that table, not by copying a method.
- **Node execution uses a bounded ready-set scheduler.** `Executor._run`
  dispatches independent ready nodes concurrently up to
  `max_concurrent_nodes`; setting it to `1` preserves sequential execution.
  Dependency ordering and branch-control edges remain deterministic. See
  `docs/specs/parallel-execution.md`.
- **DAG acyclicity is a permanent, load-bearing invariant.** Plans are directed
  *acyclic* graphs; the validator enforces topological sort and cycle detection.
  Iteration is expressed *within* a node (a future `foreach` node fans out over
  a list using the subplan mechanism), never as a graph cycle. Cycles would
  break the validator, the topological executor, the ACP plan mapping, and the
  OTel span tree simultaneously. Do not add cycles to the plan grammar.
  Conditional loops ("repeat until X") belong at the orchestrator/session layer,
  not the plan layer. See `vault/Spec - Foreach Node (Loops).md`.

## Package map

```
src/glassrail/
├── core/       Plan, Node, NodeStatus, TaskId, errors (imports nothing else)
├── config/     pydantic-settings
├── events/     typed events + EventBus
├── providers/  LLMProvider Protocol, TierRouter, OpenAI-compat impl, factory
├── state/      StateStore Protocol + memory + sqlite
├── harness/    ToolHarness, decorator, entry-point loader, builtin tools
├── validator/  PlanValidator + invariants (topo sort, nesting, subplan caps)
├── planner/    plan generation (JSON-mode prompt)
├── executor/   topological execution, branch logic, HITL, Orchestrator
├── channels/   chat / task / job (design only)
├── gateways/   rest (FastAPI); later telegram, tui
├── telemetry/  OpenTelemetry tracing (setup + span vocabulary)
└── cli/        Typer entry point
```

`core/` must not import from any other `glassrail` subpackage. Everything may
import `core`.

## Polyglot: `clients/`

The repo is a polyglot monorepo. `clients/tui/` is a **Rust** crate
(`glassrail-tui`) — the terminal client built on ratatui, talking to the agent
over ACP by spawning `glassrail acp`. The Python agent core is unchanged; the
client is a separate binary behind the protocol seam. Requires a Rust toolchain
(`rustup`, stable). Its own check sweep, run from `clients/tui/`:

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo build --locked
cargo test --locked
```

CI runs these in a dedicated `rust-tui` job; the Python jobs are unchanged. See
`clients/tui/README.md`.

## Tests

`asyncio_mode = "auto"` — write `async def test_...` directly, no marker needed.
Layout under `tests/`:

- `unit/` — fast, isolated.
- `integration/` — multiple real components (real planner/executor/validator
  with a scripted fake provider).
- `contract/` — shared suites every plugin impl must pass (add a backend to the
  parametrisation, get the whole suite for free).
- `property/` — hypothesis invariants (e.g. fresh-context).

Fake LLM providers in tests are scripted: they pop canned responses in order.
Grep existing tests for `_Scripted` for the pattern.

Model-quality **evals** (planner/executor behaviour, multi-trial pass@k vs
pass^k) are not pytest — they live in the standalone `eval-framework/` and run
the real agent over its tier routing via `glassrail run --json`. See
`docs/evals.md` and `eval-framework/CLAUDE.md`.

## Pull requests

- One coherent feature, bug fix, or infrastructure workstream per PR.
- Keep its implementation, required refactors, tests, docs, and changelog entry
  together. Move unrelated cleanup or follow-up work to a separate branch.
- When work depends on an unmerged change, wait for the prerequisite or open a
  clearly identified stacked PR based on that prerequisite branch.
- Every PR must be independently reviewable and green before merge.

## Commits

- Show the drafted commit message and wait for approval before committing.
- Message style: plain prose. One summary line, then ~2 lines of body (up to ~4
  when the change genuinely warrants it). No `Co-Authored-By` trailer. Don't
  leak internal phase names ("Phase 0.5") into messages.
- Branch off `main` before committing if work isn't already on a branch.
