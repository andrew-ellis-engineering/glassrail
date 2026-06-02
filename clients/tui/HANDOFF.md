# TUI / ACP — handoff

Status as of commit `1bd7881` on branch **`acp-tui`**. This covers the Rust
terminal client (`clients/tui`) and the Python ACP adapter (`dagagent acp`) that
backs it. Read this top-to-bottom before touching the code; it captures the
contract and the gotchas that aren't obvious from the source.

> The original design spec is at `~/vault/Dag agent ACP TUI spec.md`. This doc is
> the *current* state plus what's next; the spec is the original intent.

---

## 1. What this is

The Python agent core is unchanged. We added a **protocol seam** so a fast,
separate client can drive it:

```
clients/tui  (Rust + ratatui)
      │  JSON-RPC 2.0 over stdio  (client spawns the agent as a child)
dagagent acp   (src/dagagent/gateways/acp/)   ← ACP adapter
      │  build_runtime()  (unchanged composition root)
Orchestrator · Planner · Executor · EventBus  ← unchanged engine
```

The client spawns `dagagent acp`, performs the ACP handshake, submits tasks,
streams the plan + node execution, gates plan approval, and renders it all in
the terminal. No gateway needed.

---

## 2. Current state — DONE and green

Milestones M0–M4 from the spec, plus four polish tracks, are complete.
**30 Rust tests, 220 Python tests, all sweeps clean.**

| Area | What works |
|---|---|
| M0 adapter | `initialize`, `session/new`, `session/prompt`, `session/cancel`; EventBus → `session/update` streaming |
| M1 plan gate | `session/request_permission`; approve / reject / **reject-with-feedback → guided replan** (`Orchestrator.revise`, `Planner.plan(feedback=)`) |
| M3 dovetail | follow-up prompts in a session carry the prior result forward (`Session.compose_request`) |
| M4 cancel | `CANCELLED` status + `TaskCancelled` event; orchestrator restores state on `CancelledError`; single cancel point in the adapter |
| TUI #1 | tool args + output preview; per-node tier/confidence (`node_meta` extension) |
| TUI #2 | spinner + live elapsed timer (100 ms tick); mouse-wheel scroll |
| TUI #3 | composer cursor editing (`←/→`, `Home/End`, `Backspace/Del`); task history (`Ctrl-P/Ctrl-N`) |
| DAG view | `Tab` toggles a panel; nodes grouped into dependency **layers**, recoloured live by status (`plan_graph` extension). **No edges drawn yet.** |

Arc commits (newest first): `1bd7881` DAG view · `d9e5641` composer editing +
history · `6c03921` spinner/timer/mouse · `9bb1081` tool I/O + tier/confidence ·
`22da8bd` scrollback · `c929c3c` fake agent · `9c1093c` Outbound trait
(testability) · `2a272e6` cancellation · `7a40dd6` dovetail · `332aca7` Rust
client · `4974777` plan gate · `b0ffd33` adapter.
(`a64eed5` is unrelated — an eval-suite commit from a forked session.)

---

## 3. How to run / verify

`cargo` is **not** on PATH in fresh shells — `source "$HOME/.cargo/env"` first.

```bash
cd clients/tui

# No model server — scripted demo agent (best first look):
cargo build --example fake_agent
cargo run -- ./target/debug/examples/fake_agent
#   submit a task → approve (a) / revise (e) / reject (r); Tab = DAG view.

# Real agent (needs the MLX server on :8080):
cargo run -- uv run dagagent acp
```

Agent command resolves: positional args → `DAGAGENT_AGENT_CMD` env → default
`dagagent acp`.

**Check sweeps — both must be green before any commit:**

```bash
# Rust (from clients/tui)
cargo fmt --check
cargo clippy --all-targets -- -D warnings    # warnings are errors
cargo build --locked
cargo test --locked

# Python (from repo root)
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright          # zero errors AND zero warnings
```

CI runs the Rust checks in a dedicated `rust-tui` job (`.github/workflows/ci.yml`).

---

## 4. File map

**Python adapter — `src/dagagent/gateways/acp/`**
- `protocol.py` — JSON-RPC framing over stdio. `Connection`: `request` (with a
  pending-futures map for agent→client requests), `notify`, `respond`,
  `incoming()`.
- `server.py` — `AcpServer`. `dispatch()` (requests) and `handle_notification()`
  are the public seams tests drive. `_run_turn` is the turn loop (event-driven,
  with an explicit cancel `Event`); `_handle_gate` does `request_permission`;
  `_translate` maps each EventBus event → `session/update`; `_emit_node_meta`
  and `_plan_graph` are the extensions.
- `mapping.py` — `PlanTracker`: builds ACP plan entries, maps `NodeStatus` →
  ACP's 3-value status, holds tool name/args.
- `session.py` — `Session` + registry; `compose_request` does the dovetail.
- `__init__.py` — `run_acp()`; runs with `confirm_plans=True`.

**Engine touch-points (small, deliberate):** `executor/orchestrator.py`
(`revise`, `_present_or_execute`, `CancelledError` handling), `planner/planner.py`
(`feedback`), `core/execution.py` (`CANCELLED`), `events/types.py`
(`TaskCancelled`), `gateways/rest/app.py` (cancelled snapshot).

**Rust client — `clients/tui/src/`**
- `acp/client.rs` — `AcpClient` (spawns child, JSON-RPC over its stdio),
  `Outbound` **trait** (request/notify/respond — the seam that makes `App`
  testable with a fake), `ServerMessage`, `route_incoming` (+ unit tests).
- `acp/messages.rs` — typed `SessionUpdate` (serde-tagged on `sessionUpdate`),
  incl. the `node_meta` and `plan_graph` extensions; tolerant of unknown kinds.
- `app.rs` — `App<O: Outbound>`: the state machine. Transcript cells, plan,
  graph, composer+cursor+history, scrollback, spinner/elapsed, modes
  (Normal/Approval/Feedback), `show_dag`. **~20 unit tests live here.**
- `ui.rs` — all rendering. Generic only at `render()`; helpers take concrete data.
- `graph.rs` — DAG layering (ported `_layers`); `build`, `max_layer` (+ tests).
- `transcript.rs` — the `Cell` enum.
- `main.rs` — tokio loop (`select!` over input / agent messages / 100 ms tick),
  handshake, mouse capture, agent-command resolution.
- `examples/fake_agent.rs` — canned ACP agent for MLX-free runs and as a living
  reference for the wire.

---

## 5. The protocol contract (read before changing either side)

Standard ACP we implement: `initialize`, `session/new`, `session/prompt`,
`session/cancel`, `session/update` (kinds: `plan`, `tool_call`,
`tool_call_update`, `agent_message_chunk`), `session/request_permission`.
**Skipped** (advertised unsupported): `fs/*`, `terminal/*`, `session/load`.

**dagagent extensions** — custom `session/update` kinds; a generic ACP client
ignores unknown kinds, ours renders them:
- `node_meta` `{nodeId, nodeType, tier, confidence, flagged}` — emitted on node
  completion (completed/failed only, not skipped). Client shows a dim
  tier/confidence line; **suppressed for the `result` node** (always conf 1.0).
- `plan_graph` `{nodes: [{id, nodeType, description, deps}]}` — the graph
  topology, sent once per plan because ACP's `plan` is a flat list with no edges.

**Other contract points:**
- Tool I/O: `tool_call` carries `rawInput` (node args); `tool_call_update`
  carries `rawOutput` `{output: <text>}`.
- **Reject-with-feedback** is an extension on the permission *response*: a
  free-text `feedback` field (top-level or inside `outcome`). Present → adapter
  calls `Orchestrator.revise` and re-gates; absent → plain reject (`refusal`).
- The adapter runs `confirm_plans=True`, so every turn pauses at the gate.

**Ordering invariant (important):** `plan` entries, `plan_graph` nodes, and the
plan's `sorted_node_ids` are all in the **same topological order**. The client
syncs per-node graph status from `plan` entries **by position**, relying on this.
If you ever reorder one, fix all three.

---

## 6. Next TUI work (in suggested order)

1. **DAG edge drawing** — the deferred 20% of the DAG view. The layered
   structure, status colours, and toggle are done; what's missing is the
   box-drawing connectors between layers. **Port the routing from
   `src/dagagent/gateways/tui/dag.py`** (`_compute_layout`, `_Layout`,
   `_layers`): it splits multi-layer edges with dummy pass-through vertices so
   every segment connects adjacent layers, then draws orthogonal connectors
   through the channel between layers. The client already has the layer
   assignment (`graph.rs`); you'll need node x-positions per layer and a char
   grid to draw onto. Consider sending decision→branch **control edges** too —
   the Python `_layers` includes `branch_targets`, but `plan_graph` currently
   sends only `context_needed` data deps.

2. **Token-level streaming** (roadmap, Phase 1/2) — currently node-level. Adapter
   would emit `agent_message_chunk`s as the provider streams; the client appends
   to a live "streaming cell" that finalizes on node completion (Codex's model).
   Needs provider deltas surfaced through the EventBus first.

3. **Live validation against real `dagagent acp` + MLX** — only the fake agent
   has been exercised interactively (no TTY + busy MLX during this arc). Do a
   real run, watch streaming/gate/cancel, fix anything that surfaces.

4. **Smaller polish, if wanted:**
   - Multi-line composer input (deferred: needs growing-composer layout +
     cross-line cursor math).
   - Wrap-aware scrolling — current `scrollback` uses a pre-wrap line count, so
     it's approximate when lines wrap.
   - Scroll the DAG view when it overflows (currently static, no scroll).

5. **Roadmap, further out:** TUI chat-session mode (Phase 2) — evolve from
   one-shot turns into a persistent chat surface; depends on the channels work.

---

## 7. Gotchas / conventions

- **Mouse capture is on**, so terminal text-selection needs `Shift` (or it'll be
  captured as scroll/click events). Disabled cleanly on exit.
- **`App` is generic over `Outbound`** specifically so tests use a `FakeOutbound`
  with no subprocess. Keep new client→agent calls behind that trait.
- Adding a `session/update` kind: add a `SessionUpdate` variant in
  `messages.rs`; unknown kinds already parse to a dropped value, so old clients
  stay safe.
- `Tab` toggles the DAG view; it's not a composer character (don't rebind it to
  text). `↑/↓` are bound to scroll, which is why history is on `Ctrl-P/Ctrl-N`.
- **Commit style** (repo convention): plain prose, one summary line + a short
  body, **no `Co-Authored-By` trailer**, no internal phase names. Keep
  `README.md` / `CHANGELOG.md` current when the surface changes; the polyglot
  note is in `CLAUDE.md` / `AGENTS.md`.
- This `HANDOFF.md` is transient — delete it (or fold useful bits into
  `clients/tui/README.md`) once the next chunk lands.
