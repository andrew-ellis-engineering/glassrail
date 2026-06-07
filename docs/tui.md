# Terminal UI

`glassrail tui` submits a task to a running gateway and renders its progress
live in the terminal — the plan, each node as it runs, and the final output.

## Usage

Start a gateway (`uvicorn glassrail.gateways.rest:app`), then:

```bash
glassrail tui "what do I have on my calendar today?"
```

Point it at a non-default gateway with `--url`:

```bash
glassrail tui "summarise my unread mail" --url http://my-host:8000
```

The view updates as events arrive. Once the plan is ready it draws the DAG as
boxes connected by edges — nodes grouped into layers (a layer's nodes have no
dependency between them and run in parallel) — above a table with the per-node
tier and confidence. Each box is titled with its id and tool/type and shows a
short summary (the node's planner `description`); edges route between boxes, so
a fan-out, a fan-in, and a decision's branches are all visible at a glance. A
box's border colour tracks its status as the run progresses:

```
╭─ plan ─────────────────────────────────────────────────────────────╮
│ ┌──────────────────────┐    ┌──────────────────────┐               │
│ │● 1 web_search        │    │● 2 web_search        │               │
│ │Search Raft paper     │    │Search Paxos comparis…│               │
│ └───────────┬──────────┘    └───────────┬──────────┘               │
│             └─────────────┬─────────────┘                          │
│                 ┌──────────┴───────────┐                           │
│                 │◐ 3 synthesis         │                           │
│                 │Combine findings      │                           │
│                 └──────────┬───────────┘                           │
│                 ┌──────────┴───────────┐                           │
│                 │○ 4 result            │                           │
│                 │Bullet-point summary  │                           │
│                 └──────────────────────┘                           │
╰──────────────────────────────────────────────────────────────────────╯
```

The title glyph encodes status — `○` pending, `◐` running, `●` completed,
`✗` failed, `⊘` skipped (a branch not taken) — and so does the border colour.
A decision box shows its condition as the summary and the branch it took
(`→yes`), and a flagged (low-confidence) node is marked `⚑`. Edges that span
more than one layer are split with pass-through vertices so they never cross a
box. Because the grid can't reflow, a terminal too narrow for it falls back to
a compact vertical list (dependencies spelled out as `← 1,2`); pass `--no-dag`
to always show only the table.

When the task finishes it shows the final result (or the error if it failed)
and exits.

## How it works

The client is deliberately thin: it `POST`s `/task`, then consumes the SSE
event stream at `/task/{id}/events`, folding each event into a small view model
that Rich renders. The DAG is built from the plan carried on the `plan_ready`
event; the layering and colouring are a pure function of that plan plus the
node statuses accumulated from later events, so the whole view is testable
without a terminal. If you connect after the task already finished, the gateway
sends a terminal snapshot, so you still see the outcome.

Because it speaks the same HTTP + SSE the gateway already exposes, the TUI
needs nothing special server-side — and the producers (executor/orchestrator)
are unaware of it.
