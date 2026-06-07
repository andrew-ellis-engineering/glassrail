# glassrail-tui redesign spec

This spec targets the Rust `clients/tui` ACP client. It does not replace the
older Python/Rich `glassrail tui` gateway viewer, though it should port the
Python DAG edge-routing ideas where they are already better than the Rust view.

The goal is a sharp, sleek, snappy terminal client: visually intentional, dense
without being noisy, fast under streaming updates, and useful as the primary
human-in-the-loop surface for plan approval, tool approval, graph debugging, and
follow-up work.

## Design stance

The TUI should feel like a focused operator console, not a novelty terminal
skin.

- Keep the layout spatially stable. Users should learn where the graph,
  transcript, composer, and status live.
- Use color semantically, not decoratively. The UI must still work in
  monochrome and 16-color terminals.
- Prefer crisp structure over chrome. Borders should clarify regions; they
  should not be the design.
- Preserve terminal fluency. Keyboard-first, mouse-optional, and text selection
  must not feel trapped by the app.
- Animate only useful state. Spinners and live elapsed time are good; animated
  transitions that delay input are not.
- Make intermediate agent state visible but subordinate. The final answer,
  approvals, failures, and graph state should be easier to find than raw
  thinking text.

The closest design pattern is a persistent multi-panel operator console:
simultaneous graph context plus transcript detail, with a compact mode below
wide-terminal breakpoints.

## Current system map

The current Rust client works like this:

```text
glassrail-tui
  spawns child process
  JSON-RPC 2.0 over stdio
glassrail acp
  bridges EventBus events into session/update notifications
Runtime
  Orchestrator -> Planner -> Executor -> EventBus
```

Important current files:

- `clients/tui/src/main.rs`: async event loop, crossterm input, mouse wheel,
  100 ms tick, ratatui draw call.
- `clients/tui/src/app.rs`: state machine, transcript cells, plan entries,
  approval modes, composer, scrollback, graph toggle.
- `clients/tui/src/ui.rs`: status line, plan panel, transcript, DAG list,
  composer, approval/feedback overlays.
- `clients/tui/src/graph.rs`: converts `plan_graph` wire nodes into layer
  assignments. It does not draw edges yet.
- `clients/tui/src/acp/messages.rs`: typed `session/update` payloads.
- `src/glassrail/gateways/acp/server.py`: emits ACP updates and the custom
  `plan_graph` extension.
- `src/glassrail/gateways/tui/dag.py`: older Python/Rich DAG renderer with
  actual edge routing. This is the best source to port for graph edges.

What is already good:

- The client is decoupled over ACP and can run against a fake agent.
- The app is async and responsive under streamed updates.
- The transcript already distinguishes prompt, final message, synthesis,
  thought, tool, metadata, and notice cells.
- The composer already wraps visually and grows up to a capped height.
- Plan and tool approval both use the same permission overlay path.
- The graph topology exists, but only as grouped layers.

Primary gaps:

- The graph view does not draw edges.
- The graph payload only sends data dependencies; decision branch/control edges
  are not represented.
- The graph view replaces the transcript instead of acting as persistent context
  on sufficiently wide terminals.
- `Tab` currently toggles the graph instead of cycling focus, which fights the
  common TUI convention.
- Scrollback is line-based but not a first-class viewport model across
  transcript, graph, and composer.
- Copy/select ergonomics depend on knowing terminal Shift-selection behavior.
- Markdown output is displayed as plain text.
- Thinking visibility is global and currently defaults open; it should be more
  deliberate and less visually loud.

## Proposed layout

### Wide mode: 120 columns and up

Use a persistent split layout:

```text
 glassrail  ● ready                         Tab focus  / search  ? help
┌─ graph ───────────────────────┐┌─ transcript ───────────────────────────┐
│     ┌──────────────┐          ││ ❯ user prompt                           │
│     │● 1 search    │          ││                                          │
│     │Raft overview │          ││ ⚙ web_search (query=...) [completed]    │
│     └──────┬───────┘          ││   -> result preview...                  │
│            │                  ││                                          │
│     ┌──────┴───────┐          ││ > thinking collapsed (t to expand)      │
│     │▶ 2 synth     │          ││                                          │
│     │Compare facts │          ││ ● Final answer...                       │
│     └──────────────┘          ││                                          │
└───────────────────────────────┘└──────────────────────────────────────────┘
┌─ task ────────────────────────────────────────────────────────────────────┐
│ > Do a web search for Raft and give a 3 bullet summary                    │
└────────────────────────────────────────────────────────────────────────────┘
```

Suggested split:

- Top rail: 1 line.
- Body: graph rail at 34-40 columns minimum, transcript gets the rest.
- Composer: grows from 3 to 8 lines based on wrapped input.

The graph should be visible by default once a plan exists. It is core context
for a DAG agent, not a hidden alternate screen.

### Medium mode: 80-119 columns

Use stacked panels:

```text
 glassrail  working 12s                         g graph  t thoughts  ? help
┌─ plan / graph summary ────────────────────────────────────────────────────┐
│ ● 1 search ──▶ ▶ 2 synth ──▶ · 3 result                                   │
└────────────────────────────────────────────────────────────────────────────┘
┌─ transcript ───────────────────────────────────────────────────────────────┐
│ ...                                                                        │
└────────────────────────────────────────────────────────────────────────────┘
┌─ task ────────────────────────────────────────────────────────────────────┐
│ > ...                                                                      │
└────────────────────────────────────────────────────────────────────────────┘
```

The graph panel collapses to a compact one- or two-line path/fanout summary by
default. Pressing `g` expands it to replace the transcript temporarily.

### Small mode

Below 80x24, show compact mode if possible; below roughly 60x16, show a minimum
size gate. The current 40x8 gate is technically robust but not useful for this
interface.

## Visual system

Add a small `Theme` module instead of scattering raw colors through `ui.rs`.
Use semantic style methods:

- `brand`: app label and active accent.
- `fg_default`: ordinary transcript text.
- `fg_muted`: metadata, timestamps, collapsed thinking.
- `fg_emphasis`: headings and final answer lead-in.
- `panel_border`: idle panel border.
- `panel_focus`: focused panel border.
- `status_ready`: ready/completed.
- `status_working`: running/in-progress.
- `status_blocked`: awaiting approval.
- `status_error`: failure/deny.
- `status_skipped`: skipped/branch not taken.
- `tool`: tool call accents.
- `thought`: thinking blockquote style.

Default palette direction:

- Base: terminal default background, not a hard-coded dark slab.
- Primary accent: cyan/teal for glassrail identity and focus.
- Secondary accent: amber for in-progress/attention.
- Success/error: terminal green/red, with glyph labels so color is not the only
  carrier.
- Muted: `DIM` modifier over the terminal foreground rather than hard-coded
  dark gray.

Rules:

- Respect `NO_COLOR`.
- Use terminal ANSI colors as the baseline. True color can be added later, but
  the hierarchy must not depend on it.
- Avoid purple as the default brand color.
- Use Unicode box drawing, but provide clean fallbacks for narrow terminals.

## Interaction model

Adopt conventional TUI layers:

- `Tab`: cycle focus between graph, transcript, and composer.
- `Shift-Tab`: cycle backward when crossterm exposes it; otherwise leave as a
  best-effort enhancement.
- `g`: expand/collapse graph detail in medium mode; center graph in wide mode.
- `t`: toggle the focused thinking group or all thinking when focus is not in
  a transcript cell.
- `/`: search transcript and visible graph labels.
- `n` / `N`: next/previous search hit.
- `?`: contextual help overlay.
- `Esc`: cancel active turn; close overlay/search/detail mode; quit only when
  idle with no modal state.
- `Ctrl-P` / `Ctrl-N`: prompt history, as today.
- `PageUp` / `PageDown`: scroll focused pane.
- `g` / `G` in transcript focus: top/tail. If this conflicts with graph
  shortcut, scope it by focus.
- `Alt-Enter` or `Ctrl-J`: insert newline in composer.
- `Enter`: submit from composer; confirm in approval overlays.

Open decision: whether to preserve current `Tab` as graph toggle for one more
release. Recommendation: change it now, while the TUI is still early. Put the
old behavior behind `g` and update the footer/help copy.

## Graph and edge drawing

### ACP payload

The current `plan_graph` extension sends nodes with `deps`, where `deps` are
derived only from `context_needed`. That is enough for data-flow edges but not
for decision branch/control edges. It also forces the client to infer edges
from node fields.

Evolve the payload in a backward-compatible way:

```json
{
  "sessionUpdate": "plan_graph",
  "nodes": [
    {
      "id": 1,
      "nodeType": "tool",
      "description": "Search Raft overview",
      "deps": []
    }
  ],
  "edges": [
    {"from": 1, "to": 2, "kind": "data"},
    {"from": 3, "to": 4, "kind": "control", "label": "yes"}
  ]
}
```

Compatibility:

- Keep `deps` on nodes so older clients still work.
- New clients prefer explicit `edges`.
- If `edges` is absent, derive data edges from `deps`.
- Standard ACP clients still ignore the unknown `plan_graph` update.

Server-side edge source:

- Data edges: every valid `context_needed` entry.
- Control edges: every decision branch target from `branches`.
- Optional labels: branch key (`yes`, `no`, `fallback`) for decision control
  edges.

### Rust graph model

Extend `clients/tui/src/graph.rs` from "layer assignment only" to "layout":

- `GraphNode`: id, type, description, status, flagged, tier/confidence
  metadata if available later.
- `GraphEdge`: from, to, kind (`data` or `control`), optional label.
- `GraphLayout`: layers, x/y positions, segments, width, height.
- `GraphCell` or `Grid`: char plus style plus connector direction bitmask.

Stop syncing graph status by plan-entry position. Sync by node id. The current
position invariant is fragile and makes future graph payload enrichment risky.

### Layout algorithm

Port the proven Python/Rich renderer from `src/glassrail/gateways/tui/dag.py`.

Algorithm:

1. Build the edge set from explicit `edges` or derived `deps`.
2. Assign each node to a layer equal to the longest path from a root.
3. Split every edge that spans more than one layer with dummy pass-through
   vertices so every rendered segment connects adjacent layers.
4. Place nodes left-to-right within each layer, then center each layer against
   the widest layer.
5. Draw fixed-size node boxes onto a grid.
6. Route orthogonal connectors through the channel between adjacent layers.
7. Use direction bitmasks to resolve intersections into correct box-drawing
   glyphs.

Initial geometry:

- Node box width: 24 columns.
- Node box height: 4 rows.
- Horizontal gap: 4 columns.
- Channel height between layers: 2 rows.

Node box content:

```text
┌──────────────────────┐
│● 1 web_search     ⚑  │
│Search Raft overview  │
└──────────┬───────────┘
```

Status glyphs:

- Pending: `·`
- Running: `▶`
- Completed: `●` or `✓`
- Failed: `✗`
- Skipped: `⊘`
- Awaiting approval: `!` or paused indicator

Use labels sparingly. A decision branch label can appear in the edge channel
when it fits; otherwise render it in the selected-node detail line.

### Graph viewport

The graph can overflow both horizontally and vertically. Add a graph viewport:

- Arrow keys pan graph when graph panel is focused.
- `Home` / `End`: left/right edge.
- `g`: center selected/current node.
- `f`: fit/compact fallback toggle.
- Mouse wheel scrolls focused pane vertically.

Fallback when graph is wider than the panel:

```text
layer 0
  ● 1 web_search  Search Raft overview
layer 1
  ▶ 2 synthesis   Combine results  <- 1
layer 2
  · 3 result      Write bullets    <- 2
```

This fallback should be intentionally styled, not treated as an error.

## Transcript and output rendering

### Cell hierarchy

Keep the existing transcript cell model, but render cells with clearer roles:

- Prompt: cyan prompt marker, bold first line.
- Tool call: compact header, args preview collapsed by default, output preview
  expandable.
- Thought: collapsed by default, live spinner while the node is streaming,
  dim/italic blockquote styling when expanded.
- Synthesis/summary: visible intermediate work, less muted than thoughts.
- Final result: highest weight. Render after a small label like `answer`.
- Metadata: one-line chips for tier/confidence/flagged, visually subordinate.
- Notice/error: status color plus text, not just dim prose.

Thinking default:

- Default collapsed.
- While live: `thinking 12s <spinner>` or `thinking...` with a subtle indicator.
- Expanded: render as Markdown block quote style, dim + italic:

```text
> considering source credibility...
> comparing Raft and Paxos...
```

### Markdown

Render common Markdown in the transcript instead of showing raw Markdown.

Recommended dependency: `pulldown-cmark`, converted into ratatui `Line`s.

Support first:

- Headings: bold, maybe underlined for h1/h2.
- Bullets and numbered lists.
- Block quotes, used by thoughts.
- Fenced code blocks with a dim border or indented block.
- Inline code.
- Emphasis and strong emphasis.
- Links: underlined label plus dim URL if useful.

Do not aim for full CommonMark perfection in the first pass. The acceptance bar
is that ordinary model output looks structured, wraps correctly, and remains
copyable as plain text.

### Scroll model

Replace per-render ad hoc scroll math with explicit pane viewports:

- `TranscriptViewport { offset_from_tail, follow_tail, search_hits }`
- `GraphViewport { x, y, selected_node }`
- `ComposerViewport { first_visible_line }`

Each pane should own its visual-line cache for the current width. Recompute
only when content or width changes.

Requirements:

- Long final answers must be scrollable to the last visual line.
- New streamed output follows the tail only if the user has not scrolled up.
- When the user scrolls up, show a clear "scrolled" marker and do not yank them
  back to the tail until they press `G`, `End`, or submit a new prompt.
- Mouse wheel scrolls the focused pane, not always the transcript.

## Copy and selection ergonomics

Terminal text selection and mouse capture are naturally at odds. Do not pretend
otherwise; make it explicit and easy.

Add a copy/select mode:

- Key: `c` from transcript focus.
- Behavior: temporarily disable mouse capture, freeze live auto-scroll, simplify
  the footer to `select text with mouse; Esc returns`.
- Exit: `Esc` re-enables mouse capture and returns to the previous focus.
- Preserve `Shift-drag` terminal selection as a documented always-available
  fallback.

Optional later enhancement:

- `Y`: copy the final answer or selected transcript cell through OSC 52 when
  supported and enabled. Keep this optional because terminal clipboard support
  is uneven and can surprise users over SSH/tmux.

## Approval overlays

Plan approval and tool approval should use the same visual grammar but different
content hierarchy.

Plan approval:

- Title: `approve plan`
- Body: compact plan list with status glyphs and node types.
- Actions: approve, reject, revise.
- Revise feedback box should support wrapping and basic cursor editing.

Tool approval:

- Title: `approve tool call`
- Body:
  - tool name
  - risk badge
  - node id
  - description
  - args rendered as pretty JSON or compact key/value rows
- Actions: allow once, always allow, deny.
- Risk styling:
  - read: muted/info
  - write: amber
  - execute: red

Modals should trap focus and dim the underlying panels by de-emphasizing their
styles where practical. Do not add heavy "glass" effects; crisp centered panels
are enough.

## Implementation slices

### Slice 1: graph edge foundation

Files:

- `src/glassrail/gateways/acp/server.py`
- `clients/tui/src/acp/messages.rs`
- `clients/tui/src/graph.rs`
- `clients/tui/src/ui.rs`
- `clients/tui/examples/fake_agent.rs`

Work:

- Add explicit `edges` to `plan_graph`.
- Include decision branch/control edges.
- Parse optional edges in the Rust client.
- Build graph layout with dummy vertices and routed connector segments.
- Render graph grid in the graph panel.
- Add graph fallback list when too narrow.
- Update fake agent to include at least one diamond/fan-in graph.

Acceptance:

- Diamond graph renders fan-out and fan-in connectors.
- Long edge spanning multiple layers routes through dummy vertices.
- Decision branch edge appears as a control edge when branches are present.
- Existing clients remain compatible because `deps` still exists.

### Slice 2: responsive shell and focus

Files:

- `clients/tui/src/app.rs`
- `clients/tui/src/ui.rs`
- `clients/tui/src/main.rs`

Work:

- Add focus enum: graph, transcript, composer.
- Change `Tab` to cycle focus and move graph toggle to `g`.
- Add wide/medium/small layout breakpoints.
- Show graph persistently in wide mode.
- Add focused border styles and contextual footer hints.

Acceptance:

- 80x24, 120x40, and 200x60 all render intentionally.
- `Tab` focus is visible and predictable.
- Existing submit, approval, cancel, and history flows still work.

### Slice 3: transcript rendering, Markdown, and scrolling

Files:

- `clients/tui/src/transcript.rs`
- `clients/tui/src/ui.rs`
- optional new `clients/tui/src/markdown.rs`
- optional new `clients/tui/src/viewport.rs`

Work:

- Add visual-line caches and pane viewport structs.
- Make thoughts collapsed by default.
- Add thought live spinner styling.
- Render Markdown for final and synthesis cells.
- Ensure long final results fully scroll.

Acceptance:

- Long Markdown answers with bullets, code fences, and headings render cleanly.
- Scrollback does not clip the final result.
- User scrolling up disables tail-follow until explicitly returning.

### Slice 4: copy/select and modal polish

Files:

- `clients/tui/src/main.rs`
- `clients/tui/src/app.rs`
- `clients/tui/src/ui.rs`

Work:

- Add select/copy mode that disables mouse capture.
- Improve plan/tool approval overlays.
- Add wrapped/cursor-editable feedback input.
- Document `Shift-drag` and copy mode.

Acceptance:

- User can enter copy/select mode, drag terminal text, and return cleanly.
- Mouse wheel behavior returns after leaving copy/select mode.
- Tool approvals show risk, args, and allow/deny options clearly.

### Slice 5: docs, tests, and polish pass

Files:

- `clients/tui/README.md`
- `clients/tui/HANDOFF.md` or replacement durable docs
- unit tests in `clients/tui/src/*`
- integration/fake-agent coverage as needed

Work:

- Update key table and layout docs.
- Add render/layout tests using ratatui test backend where practical.
- Add graph algorithm tests for chains, diamonds, fan-in, long edges, and
  branch/control edges.
- Add ACP adapter tests for `plan_graph.edges`.
- Add fake-agent scenario that exercises graph edges, thinking, Markdown, and
  tool approval.

Acceptance:

- Rust checks pass:
  - `cargo fmt --check`
  - `cargo clippy --all-targets -- -D warnings`
  - `cargo build --locked`
  - `cargo test --locked`
- Python checks pass if ACP server payload changes:
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run pyright`

## Test plan

Graph tests:

- Chain: `1 -> 2 -> 3`.
- Diamond: `1 -> {2,3} -> 4`.
- Long edge: `1 -> 4` with layers 2 and 3 populated by other nodes.
- Multi-parent fan-in with crossing-prone positions.
- Decision branch: decision node routes to branch target with control edge.
- Unknown/missing edge endpoints are ignored safely.
- Cycles fail soft in the renderer and should never panic.

UI tests:

- Minimum-size gate.
- Wide split layout chooses graph + transcript.
- Medium layout chooses compact graph summary.
- Composer height grows and scrolls internally.
- Transcript viewport honors tail-follow vs scrolled-up mode.
- Thoughts collapsed by default.
- Approval overlay uses plan vs tool content correctly.

Manual smoke tests:

- Fake agent, no model server.
- Real `uv run glassrail acp` with local model when available.
- Resize at 80x24, 120x40, 200x60.
- tmux with mouse on and off.
- Terminal selection with Shift-drag and copy/select mode.
- `NO_COLOR=1`.

## Non-goals for this pass

- Switching the Rust client to Textual. The installed Textual skill is useful
  for design patterns, but this client is intentionally Rust/ratatui.
- Structural plan editing. The UI should be ready for it later, but this pass
  only approves, rejects, and revises with feedback.
- Full CommonMark conformance.
- Persistent session loading.
- Replacing ACP or adopting a different protocol crate.

## Recommendation

Start with Slice 1. Graph edges are the highest leverage improvement because
they make the DAG agent's core abstraction visible. While implementing it, add
the explicit `edges` payload so the graph can eventually show both data and
control flow. Then do Slice 2 so the graph becomes persistent context instead
of a hidden alternate screen. Transcript Markdown and copy/select mode should
come next because they make the TUI feel like a daily-driver surface rather
than a demo viewer.
