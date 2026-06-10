# Glassrail

A DAG-planning agent with deterministic tier routing, fresh context per node, and plan-as-document semantics.

Every task becomes a validated graph of nodes instead of a ReAct loop: the
planner emits a DAG, the validator checks its invariants, and the executor runs
it topologically. Each node sees only the upstream outputs it declared it needs,
and model selection falls through an ordered set of tiers (local → cloud) by
fixed rules rather than by the model's discretion.

## Status

Early development — the engine runs end to end (plan → validate → execute over
tier routing, with persistence, a typed event stream, and a REST gateway), and
the [eval framework](./eval-framework) measures it. The Phase 1 eval gate is
met and the first PyPI release is being prepared. Treat APIs as unstable while
Glassrail is in 0.x. See [CHANGELOG.md](./CHANGELOG.md) for what's landed and
[docs/roadmap.md](./docs/roadmap.md) for what's next.

## Principles

- **DAG planning** — every task is a validated graph of nodes, not a ReAct loop.
- **Fresh context per node** — each node sees only the upstream outputs it declared in `context_needed`.
- **Plan as document** — plans are inspectable, replayable, and visualizable.
- **Tiered model routing** — deterministic fallthrough from local → cloud, with per-tier timeouts.

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) for dependency management
- **A model backend** (see below) — the agent does nothing without one.

## Quickstart

From PyPI, once the first release is published:

```bash
uvx glassrail --help
uvx glassrail run "summarise the CAP theorem in three bullets"
```

From source:

```bash
uv sync --all-extras
```

The agent needs at least one reachable LLM tier. By default **tier 0** points at
a local OpenAI-compatible server (`http://localhost:8080/v1`, model
`qwen3.6-35b-moe`) and **tiers 1–3** point at OpenRouter. Pick one:

```bash
# Option A — a local server on :8080 (e.g. an MLX or llama.cpp OpenAI-compatible
# endpoint). Nothing else to configure; tier 0 is the default.

# Option B — use OpenRouter for the cloud tiers:
export GLASSRAIL_TIER1__API_KEY=sk-or-...
```

Then run a task:

```bash
uv run glassrail run "summarise the CAP theorem in three bullets"
```

If no tier is reachable, the router walks tier 0 → tier 3 and then fails with
`All providers exhausted; last error: …` — that almost always means no model
backend is wired up, not a bug.

## Ways to run it

**Headless, one-shot** — the full engine in one process; prints the result:

```bash
uv run glassrail run "<task>"
uv run glassrail run "<task>" --json            # machine-readable result envelope
uv run glassrail run "<task>" --model <name>    # override tier 0's model
uv run glassrail run "<task>" --timeout 120     # wall-clock budget in seconds
```

The `--json` envelope includes the accepted `plan` when planning succeeds and
`planning_attempts` for every planner try, including raw model output plus parse
or validation errors. This makes failed plans inspectable from headless runs and
eval artefacts.

**Gateway + live viewer** — start the REST gateway, then watch a task stream:

```bash
uv run uvicorn glassrail.gateways.rest:app      # serves on :8000
uv run glassrail tui "<task>"                   # POSTs the task, renders the live DAG + stream
```

The viewer draws the plan as colour-coded node boxes connected by edges
(grouped into dependency layers, each box showing a short summary, recoloured as
they run) above a per-node table; `--no-dag` shows the table alone. See
[docs/tui.md](./docs/tui.md).

**Editor / agent clients (ACP)** — speak the [Agent Client
Protocol](https://agentclientprotocol.com) as a JSON-RPC 2.0 server over stdio,
so an ACP client (the in-repo Rust TUI, or Zed) can spawn the agent as a
subprocess, submit tasks, and watch the plan and nodes stream back:

```bash
uv run glassrail acp                            # JSON-RPC over stdin/stdout; logs to stderr
```

The in-repo Rust terminal client speaks this protocol — submit a task, watch the
plan stream, approve or revise it, all in the terminal. See
[clients/tui](./clients/tui/README.md):

```bash
cd clients/tui && cargo run -- uv run glassrail acp
```

It implements `initialize`, `session/new`, `session/prompt`, and
`session/cancel`; the plan and per-node execution arrive as `session/update`
notifications. Before executing, the agent pauses at a plan gate and asks the
client to approve via `session/request_permission` — a client may approve, or
reject with free-text feedback to trigger a guided replan. (`fs/*` and
`terminal/*` are intentionally unsupported — the agent runs its own tools
server-side.)

**REST API directly** — `POST /task` returns a `task_id`; follow it over
Server-Sent Events or a WebSocket at `/task/{id}/events`, or poll
`GET /task/{id}`. See [docs/streaming.md](./docs/streaming.md).

## Configuration

Twelve-factor: environment variables (and an optional `.env` / `config.toml`),
parsed by `pydantic-settings`. Tiers are nested, so use the `__` delimiter:

| Setting | Env var | Default |
|---|---|---|
| Tier 0 model | `GLASSRAIL_TIER0__MODEL` | `qwen3.6-35b-moe` |
| Tier 0 endpoint | `GLASSRAIL_TIER0__BASE_URL` | `http://localhost:8080/v1` |
| Tier 0 timeout (s) | `GLASSRAIL_TIER0__TIMEOUT_S` | `10.0` |
| Tier 1 API key | `GLASSRAIL_TIER1__API_KEY` | *(empty)* |
| HITL plan gate | `GLASSRAIL_CONFIRM_PLANS` | `false` |
| Tool approval mode | `GLASSRAIL_TOOL_APPROVAL__MODE` | `interactive` |
| Planner stall char multiplier | `GLASSRAIL_PLANNER_STALL_CHAR_MULTIPLIER` | `4` |
| Load tool plugins | `GLASSRAIL_LOAD_TOOL_PLUGINS` | `false` |

Tiers 1–3 default to OpenRouter models; override any field the same way. With a
local model as your only tier, raise `GLASSRAIL_TIER0__TIMEOUT_S` (e.g. to `120`)
— a large local model can take longer than the 10 s default, and a timeout is
treated as the tier being unavailable.

### Generation ceiling

`max_generation_tokens` (default `20000`) is a hard cap on `max_tokens` sent to
any tier for any single request, applied by the router before the request leaves
the process. Per-node budgets (below) are the goal; this is the safety backstop
that prevents a single generation from consuming unbounded memory on a local
model across long multi-step runs. Set it in `config.toml` or via
`GLASSRAIL_MAX_GENERATION_TOKENS`.

### Per-node token budgets

Each node runs with a fresh context; these cap how many tokens it may *generate*
(output), so reasoning and summaries get room while structured micro-calls stay
small. Override any field under `[budgets]` in `config.toml` (or
`GLASSRAIL_BUDGETS__<FIELD>`):

| Budget | Default | Used by |
|---|---|---|
| `planner` | 16384 | the full plan JSON |
| `think` | 8192 | multi-step reasoning |
| `summary` | 8192 | high-fidelity document/webpage summaries |
| `synthesis` | 4096 | combining prior outputs |
| `result` | 4096 | the final answer |
| `decision` | 256 | a branch label |
| `extract_args` | 512 | a tool-args object |
| `shape_check` | 128 | a yes/no output gate |

These are *output* caps. How much a node can *read* is bounded by your served
model's context window, not by these.

Planner output that is not valid JSON and exceeds
`budgets.planner * planner_stall_char_multiplier` characters is classified as a
stall; the next retry sees a truncated copy of that raw output and is told not
to repeat it.

### Node prompts

Each node role (planner, decision, think, synthesis, summary, result, and the
tool-output shape check) has a system prompt you can override without editing
source — under `[prompts]` in `config.toml` or `GLASSRAIL_PROMPTS__<FIELD>`. The
defaults live in `glassrail.config.prompts`. A custom prompt must keep
instructing the model to emit the JSON shape its node expects (e.g. a summary
prompt must still ask for `{"summary": ..., "confidence": ...}`).

### Tools

Built-in tools (`file_read`, plus `calendar_get` / `memory_search` stubs)
always register. Add a first-party tool by decorating a function with
`@harness.tool(name=..., description=..., parameters=<JSON Schema>)`.

**First-party integrations** are bundled but opt-in, configured under
`[tools.*]`. The **web** integration needs the `web` extra and is off by default:
- `web_fetch(url)` — fetch a page and extract its main text (for reading or
  summarising webpages).
- `web_search(query)` — search the web behind a pluggable provider:
  `duckduckgo` (no setup) or `searxng` (point at a self-hosted instance).

The **image** integration wraps the `mflux-generate` CLI on macOS and is also
off by default:
- `image_generate(prompt, output_path)` — generate a PNG from text using Flux.
- `image_generate(..., image_path=..., image_strength=...)` — image-to-image
  generation/editing from an existing source image.

Install `mflux` separately, then either put `mflux-generate` on `PATH` or set
`mflux_bin`. The tool is declared `write` risk, so it asks for approval in
interactive mode unless explicitly allowed.

```bash
uv sync --extra web                      # installs trafilatura + lxml
```
```toml
[tools]
fs_roots = ["~/work", "/tmp/glassrail-eval"] # optional path confinement for file tools

[tools.web]
fetch = true
search = "duckduckgo"                    # or "searxng" (+ searxng_url)

[tools.image]
enabled = true
mflux_bin = "~/.venvs/mflux/bin/mflux-generate" # optional; empty = auto/PATH
```

`tools.fs_roots` confines first-party filesystem paths after `~` expansion and
symlink resolution. When unset, file tools keep the current unconfined behavior
and log a warning the first time they resolve a path.

### Tool Approval

Per-tool approval is configured under `[tool_approval]`. Policies are:
`allow` (run), `ask` (prompt an interactive client), and `deny` (never run).
Explicit per-tool overrides win. Without an override, tools declared as
`write` or `execute` risk default to `ask`; `read` and `network` tools use the
configured `default`.
`mode = "auto"` treats `ask` as `allow` for unattended/headless execution, but
keeps `deny` as `deny`.

```toml
[tool_approval]
default = "allow"
mode = "interactive"                     # or "auto"

[tool_approval.overrides]
file_write = "ask"
shell_exec = "deny"
image_generate = "allow"                 # explicit override for a write-risk tool
```

**Third-party plugins** advertised through the `glassrail.tools` entry-point
group are a separate opt-in: set `GLASSRAIL_LOAD_TOOL_PLUGINS=true` (or
`load_tool_plugins = true`) and the runtime discovers and registers them at
startup.

### Security notes

Glassrail is early 0.x software run by its operator, not a hardened service.
Current posture (hardening is tracked in
[docs/specs/security-baseline.md](./docs/specs/security-baseline.md)):

- The REST gateway has **no authentication** — keep it bound to localhost and
  do not expose it to untrusted networks.
- `file_read` and `image_generate` output paths are confined when
  `tools.fs_roots` is set. When it is unset, file tools can access any path the
  process can access and log a one-time warning.
- The web tools fetch model-chosen URLs — enabling them means the agent has
  outbound network access it chooses how to use.
- Tool `risk` levels participate in approval: without an explicit override,
  `write` and `execute` tools ask in interactive mode. `mode = "auto"` still
  treats `ask` as `allow`, so use explicit `deny` overrides for tools that
  must never run unattended.

## Evals

Model-quality evals (multi-trial **pass@k** capability vs **pass^k** reliability
against the real agent) live in the standalone [`eval-framework/`](./eval-framework):

```bash
cd eval-framework && python3 run.py suite suites/glassrail
```

The `glassrail-cli` backend drives the real planner and executor over the agent's
own tier routing via `glassrail run --json`. See [docs/evals.md](./docs/evals.md).

## Development

```bash
uv sync --all-extras --group dev
uv run pre-commit install
uv run pytest
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full check sweep and PR
guidelines, [CLAUDE.md](./CLAUDE.md) for the package layout and conventions, and
[docs/](./docs) for the architecture, streaming, observability, and deployment
references.

## License

Apache-2.0. See [LICENSE](./LICENSE).
