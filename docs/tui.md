# Terminal UI

`dagagent tui` submits a task to a running gateway and renders its progress
live in the terminal — the plan, each node as it runs, and the final output.

## Usage

Start a gateway (`uvicorn dagagent.gateways.rest:app`), then:

```bash
dagagent tui "what do I have on my calendar today?"
```

Point it at a non-default gateway with `--url`:

```bash
dagagent tui "summarise my unread mail" --url http://my-host:8000
```

The view updates as events arrive:

```
╭─ dagagent ───────────────────────────────────────────────╮
│ task what do I have today?                                │
│ status: executing   nodes: 2                              │
│  node  type     tier  status      conf                    │
│     1  tool        0  completed   1.00                    │
│     2  result      0  running                             │
╰───────────────────────────────────────────────────────────╯
```

When the task finishes it shows the final result (or the error if it failed)
and exits.

## How it works

The client is deliberately thin: it `POST`s `/task`, then consumes the SSE
event stream at `/task/{id}/events`, folding each event into a small view model
that Rich renders. If you connect after the task already finished, the gateway
sends a terminal snapshot, so you still see the outcome.

Because it speaks the same HTTP + SSE the gateway already exposes, the TUI
needs nothing special server-side — and the producers (executor/orchestrator)
are unaware of it.
