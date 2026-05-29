# Working in this repo

`dagagent` is a DAG-planning agent: every task becomes a validated graph of
nodes, each node runs with fresh context, and tier routing is deterministic.
This file is the operating manual for working in the repo. For *what's built*
and *what's next*, see `CHANGELOG.md` and `docs/roadmap.md` — don't duplicate
them here.

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

- **Layout:** src-layout (`src/dagagent/...`), one repo, subpackages.
- **Python:** 3.12+ floor. **Async:** stdlib `asyncio` only (no anyio).
- **Deps:** `uv` with `uv.lock`. **Lint/format:** `ruff`. **Types:** `pyright`
  strict. Add a `# noqa`/rule-disable only with a reason, and prefer
  restructuring the code over suppressing.
- **IDs:** ULID. **Config:** `pydantic-settings` (env + `.env` + `config.toml`).
  **CLI:** Typer. **Logging:** stdlib `logging` + `structlog`.
- **License:** Apache-2.0. **Versioning:** SemVer 0.x. **No PyPI publish** until
  after the Phase 1 eval gates — GitHub releases only.
- **Docs:** MkDocs + Material (`mkdocs.yml`). **Observability:** structured
  logs plus OpenTelemetry tracing (`dagagent.telemetry`) — a no-op until
  configured; SDK + OTLP exporter in the optional `otel` extra. See
  `docs/observability.md`.

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
  `dagagent.tools` entry-point group (third-party).
- **Node types:** `tool`, `decision`, `synthesis`, `think`, `summary`,
  `result`, `subplan`. The four single-LLM-call types (synthesis/think/summary/
  result) share `Executor._execute_llm_node` via `_LLM_NODE_SPECS` — add a new
  one by extending that table, not by copying a method.

## Package map

```
src/dagagent/
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

`core/` must not import from any other `dagagent` subpackage. Everything may
import `core`.

## Tests

`asyncio_mode = "auto"` — write `async def test_...` directly, no marker needed.
Layout under `tests/`:

- `unit/` — fast, isolated.
- `integration/` — multiple real components (real planner/executor/validator
  with a scripted fake provider).
- `contract/` — shared suites every plugin impl must pass (add a backend to the
  parametrisation, get the whole suite for free).
- `property/` — hypothesis invariants (e.g. fresh-context).
- `eval/` — the eval harness: scores planner + executor runs against fixtures
  and prints a summary. Excluded from the default sweep; run it on its own with
  `uv run pytest -m eval`. See `docs/evals.md`.

Fake LLM providers in tests are scripted: they pop canned responses in order.
Grep existing tests for `_Scripted` for the pattern.

## Commits

- Show the drafted commit message and wait for approval before committing.
- Message style: plain prose. One summary line, then ~2 lines of body (up to ~4
  when the change genuinely warrants it). No `Co-Authored-By` trailer. Don't
  leak internal phase names ("Phase 0.5") into messages.
- Branch off `main` before committing if work isn't already on a branch.
