# dagagent-tui

A fast terminal client for [dagagent](../../README.md), built on
[ratatui](https://ratatui.rs) over the **Agent Client Protocol (ACP)**.

It spawns `dagagent acp` as a subprocess, performs the ACP handshake, and runs
an interactive loop: submit a task, watch the plan and node execution stream in,
approve or reject-with-feedback the plan, and read the result — all in the
terminal, with no gateway to run.

## Build & run

Requires a Rust toolchain (`rustup`, stable). The client spawns the agent, so
the agent command must be resolvable.

```bash
cd clients/tui
cargo run                          # spawns `dagagent acp` (must be on PATH)
cargo run -- uv run dagagent acp   # or run the agent via uv from the repo
```

The agent command resolves in this order: positional args → the
`DAGAGENT_AGENT_CMD` environment variable (space-separated) → the default
`dagagent acp`.

## Keys

| Key | Action |
|-----|--------|
| type + `Enter` | submit a task |
| `a` | approve the plan (at the gate) |
| `e` | reject with feedback → guided replan |
| `r` | reject the plan |
| `Esc` | cancel a running turn, else quit |
| `Ctrl-C` | quit |

## Layout

- **Status line** — agent state (ready / working / awaiting approval).
- **Plan panel** — the live plan, each entry re-coloured as it runs.
- **Transcript** — prompts, streamed messages, tool calls, and notices.
- **Composer** — the task input.
- **Approval overlay** — the plan-gate modal (approve / reject / revise).

## Design

The ACP wire lives in `src/acp/` (a hand-rolled JSON-RPC 2.0 client over the
child's stdio — see `client.rs` for why it isn't Zed's `agent-client-protocol`
crate yet). `app.rs` is the state machine, `ui.rs` the rendering, `main.rs` the
async event loop multiplexing terminal input and agent messages.
