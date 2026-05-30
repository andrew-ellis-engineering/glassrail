"""Render a plan as a live, colour-coded DAG for the TUI.

Pure and transport-free, like the :class:`~dagagent.gateways.tui.view.TaskView`
that calls it: given the serialised plan dict (from the ``plan_ready`` event)
and the current per-node status, draw the graph.

Nodes are grouped into topological *layers* — every node in a layer depends
only on earlier layers, so nodes sharing a layer have no edge between them and
run in parallel. Each node is a box whose border colour tracks its status, and
edges are routed as orthogonal box-drawing connectors through the channel
between adjacent layers. Edges that span more than one layer are split with
pass-through *dummy* vertices (the standard layered-graph trick) so every drawn
segment connects adjacent layers and never has to cross a box.

The diagram is a custom Rich renderable: it lays itself out onto a character
grid and, because that grid can't reflow, falls back to a compact vertical list
when it would be wider than the terminal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.text import Text

# Normalised node status -> (glyph, Rich style). The view's row statuses
# ("running", "completed", "skipped", "failed", "empty") map onto these; a
# node the run has not reached yet has no row and is treated as "pending".
_STATUS_GLYPH: dict[str, tuple[str, str]] = {
    "pending": ("○", "dim"),
    "running": ("◐", "bold yellow"),
    "completed": ("●", "bold green"),
    "empty": ("◌", "yellow"),
    "skipped": ("⊘", "dim red"),
    "failed": ("✗", "bold red"),
}
_UNKNOWN_GLYPH = ("•", "white")

# Box geometry and layer spacing, in character cells.
_BOX_W = 24
_BOX_H = 4  # top border, title, summary, bottom border
_COL_GAP = 4  # horizontal space between items in a layer
_CHANNEL = 2  # rows between one layer's boxes and the next — the edge channel

# Connector direction bits and their box-drawing glyphs.
_N, _S, _E, _W = 1, 2, 4, 8
_DIR_GLYPH: dict[int, str] = {
    _N | _S: "│",
    _E | _W: "─",
    _N | _E: "└",
    _N | _W: "┘",
    _S | _E: "┌",
    _S | _W: "┐",
    _N | _S | _E: "├",
    _N | _S | _W: "┤",
    _S | _E | _W: "┬",
    _N | _E | _W: "┴",
    _N | _S | _E | _W: "┼",
    _N: "│",
    _S: "│",
    _E: "─",
    _W: "─",
}


@runtime_checkable
class RowLike(Protocol):
    """The slice of a view row the DAG needs. ``_NodeRow`` satisfies it."""

    status: str
    branch_taken: str | None
    flagged: bool


@dataclass(frozen=True)
class _DagNode:
    id: int
    type: str
    description: str
    tool: str | None
    context_needed: tuple[int, ...]
    condition: str | None
    branch_targets: tuple[int, ...]
    subplan_size: int | None


def render_dag(plan: dict[str, Any], rows: Mapping[int, RowLike]) -> RenderableType:
    """Build the DAG renderable for ``plan`` at its current status.

    ``rows`` maps node id to its live status row; ids absent from it render as
    pending. Returns an empty ``Text`` if the plan has no usable node list, so
    the caller can splice the result in unconditionally.
    """
    nodes = _parse_nodes(plan)
    if not nodes:
        return Text("")
    return _DagDiagram(nodes, rows)


def plan_layers(plan: dict[str, Any]) -> list[list[int]]:
    """Group a plan's node ids into topological layers (parallel cohorts).

    Public so callers and tests can reason about a plan's rank structure
    without touching the internal node representation. Returns ``[]`` for a
    plan with no usable nodes.
    """
    return _layers(_parse_nodes(plan))


@dataclass(frozen=True)
class _Layout:
    """Computed geometry: where every vertex sits on the character grid.

    Vertices are node ids (positive) or pass-through dummies (negative). ``vx``
    is a vertex's centre column; ``vleft`` a real box's left column; ``vlayer``
    its layer; ``segments`` the adjacent-layer edges to route.
    """

    layers: list[list[int]]
    vx: dict[int, int]
    vleft: dict[int, int]
    vlayer: dict[int, int]
    segments: list[tuple[int, int]]
    width: int
    height: int


class _DagDiagram:
    """A plan laid out as boxes with routed edges; renders onto a char grid."""

    def __init__(self, nodes: list[_DagNode], rows: Mapping[int, RowLike]) -> None:
        self._by_id = {n.id: n for n in nodes}
        self._rows = rows
        self._layers = _layers(nodes)
        self._layout = self._compute_layout()

    # ── Rich protocol ─────────────────────────────────────────────────────

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        if self._layout.width > options.max_width:
            return Measurement(1, options.max_width)
        return Measurement(self._layout.width, self._layout.width)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self._layout.width > options.max_width:
            yield self._fallback()
            return
        grid = self._draw()
        yield from grid.to_lines()

    # ── Layout ────────────────────────────────────────────────────────────

    def _compute_layout(self) -> _Layout:
        vlayer = {nid: i for i, layer in enumerate(self._layers) for nid in layer}
        items = [list(layer) for layer in self._layers]

        # Split every edge into adjacent-layer segments, inserting a dummy
        # vertex per skipped layer so no segment ever crosses a box.
        segments: list[tuple[int, int]] = []
        next_dummy = -1
        for parent, child in self._edges():
            lp, lc = vlayer[parent], vlayer[child]
            prev = parent
            for layer_idx in range(lp + 1, lc):
                dummy = next_dummy
                next_dummy -= 1
                items[layer_idx].append(dummy)
                vlayer[dummy] = layer_idx
                segments.append((prev, dummy))
                prev = dummy
            segments.append((prev, child))

        # Place items left to right within each layer, then centre each row.
        vx: dict[int, int] = {}
        vleft: dict[int, int] = {}
        layer_width: list[int] = []
        for row in items:
            cursor = 0
            for item in row:
                w = _BOX_W if item > 0 else 1
                vleft[item] = cursor
                vx[item] = cursor + w // 2
                cursor += w + _COL_GAP
            layer_width.append(max(cursor - _COL_GAP, 0))

        width = max(layer_width, default=0)
        for i, row in enumerate(items):
            offset = (width - layer_width[i]) // 2
            for item in row:
                vleft[item] += offset
                vx[item] += offset

        n_layers = len(self._layers)
        height = n_layers * _BOX_H + max(n_layers - 1, 0) * _CHANNEL
        return _Layout(items, vx, vleft, vlayer, segments, width, height)

    def _edges(self) -> list[tuple[int, int]]:
        """Distinct parent→child edges: data deps plus decision→branch."""
        ids = set(self._by_id)
        edges: set[tuple[int, int]] = set()
        for node in self._by_id.values():
            for dep in node.context_needed:
                if dep in ids:
                    edges.add((dep, node.id))
            for target in node.branch_targets:
                if target in ids:
                    edges.add((node.id, target))
        return sorted(edges)

    # ── Drawing ───────────────────────────────────────────────────────────

    def _draw(self) -> _Grid:
        layout = self._layout
        grid = _Grid(layout.width, layout.height)
        for nid, layer in layout.vlayer.items():
            top = layer * (_BOX_H + _CHANNEL)
            if nid > 0:
                self._draw_box(grid, self._by_id[nid], layout.vleft[nid], top)
            else:
                for y in range(top, top + _BOX_H):  # dummy: a pass-through line
                    grid.add_dir(layout.vx[nid], y, _N | _S)
        for parent, child in layout.segments:
            self._draw_segment(grid, parent, child)
        return grid

    def _draw_box(self, grid: _Grid, node: _DagNode, x0: int, y0: int) -> None:
        glyph, style = self._glyph(node.id)
        right, bottom = x0 + _BOX_W - 1, y0 + _BOX_H - 1
        grid.put(x0, y0, "┌", style)
        grid.put(right, y0, "┐", style)
        grid.put(x0, bottom, "└", style)
        grid.put(right, bottom, "┘", style)
        for x in range(x0 + 1, right):
            grid.put(x, y0, "─", style)
            grid.put(x, bottom, "─", style)
        for y in (y0 + 1, y0 + 2):
            grid.put(x0, y, "│", style)
            grid.put(right, y, "│", style)

        row = self._rows.get(node.id)
        flag = "  ⚑" if row is not None and row.flagged else ""
        head = node.tool if node.type == "tool" and node.tool else node.type
        grid.write(x0 + 1, y0 + 1, _truncate(f"{glyph} {node.id} {head}{flag}", _BOX_W - 2), style)
        desc_style = "dim" if self._status(node.id) in ("pending", "skipped") else ""
        grid.write(x0 + 1, y0 + 2, _truncate(_body_text(node, row), _BOX_W - 2), desc_style)

    def _draw_segment(self, grid: _Grid, parent: int, child: int) -> None:
        layout = self._layout
        ax, bx = layout.vx[parent], layout.vx[child]
        top = layout.vlayer[parent] * (_BOX_H + _CHANNEL)
        cr0 = top + _BOX_H  # first channel row, just below the parent band
        cr1 = cr0 + 1  # last channel row, just above the child band

        if parent > 0:  # tee out of the real parent box's bottom border
            grid.put(ax, top + _BOX_H - 1, "┬", self._glyph(parent)[1])
        if child > 0:  # tee into the real child box's top border
            grid.put(bx, cr1 + 1, "┴", self._glyph(child)[1])

        if ax == bx:
            grid.add_dir(ax, cr0, _N | _S)
            grid.add_dir(ax, cr1, _N | _S)
            return
        grid.add_dir(ax, cr0, _N | (_E if bx > ax else _W))
        for x in range(min(ax, bx) + 1, max(ax, bx)):
            grid.add_dir(x, cr0, _E | _W)
        grid.add_dir(bx, cr0, (_W if bx > ax else _E) | _S)
        grid.add_dir(bx, cr1, _N | _S)

    def _fallback(self) -> RenderableType:
        """Compact vertical list for terminals too narrow for the grid."""
        lines: list[RenderableType] = []
        for depth, layer in enumerate(self._layers):
            lines.append(Text(f"layer {depth}", style="dim"))
            for nid in layer:
                node = self._by_id[nid]
                row = self._rows.get(nid)
                glyph, style = self._glyph(nid)
                head = node.tool if node.type == "tool" and node.tool else node.type
                line = Text("  ")
                line.append(f"{glyph} ", style=style)
                line.append(f"{nid} {head}", style=style)
                body = _body_text(node, row)
                if body:
                    line.append(f"  {_truncate(body, 40)}", style="dim")
                if node.context_needed:
                    deps = ",".join(str(d) for d in node.context_needed)
                    line.append(f"  ← {deps}", style="dim cyan")
                if row is not None and row.flagged:
                    line.append(" ⚑", style="yellow")
                lines.append(line)
        return Group(*lines)

    def _status(self, node_id: int) -> str:
        return _normalise_status(self._rows.get(node_id))

    def _glyph(self, node_id: int) -> tuple[str, str]:
        return _STATUS_GLYPH.get(self._status(node_id), _UNKNOWN_GLYPH)


class _Grid:
    """A character grid with per-cell style and overlaid connector directions."""

    def __init__(self, width: int, height: int) -> None:
        self._w = width
        self._h = height
        self._ch = [[" "] * width for _ in range(height)]
        self._st = [[""] * width for _ in range(height)]
        self._dir = [[0] * width for _ in range(height)]

    def put(self, x: int, y: int, char: str, style: str = "") -> None:
        if 0 <= x < self._w and 0 <= y < self._h:
            self._ch[y][x] = char
            self._st[y][x] = style
            self._dir[y][x] = 0

    def write(self, x: int, y: int, text: str, style: str = "") -> None:
        for i, char in enumerate(text):
            self.put(x + i, y, char, style)

    def add_dir(self, x: int, y: int, directions: int) -> None:
        if 0 <= x < self._w and 0 <= y < self._h:
            self._dir[y][x] |= directions

    def to_lines(self) -> list[Text]:
        lines: list[Text] = []
        for y in range(self._h):
            line = Text()
            for x in range(self._w):
                directions = self._dir[y][x]
                if directions and self._ch[y][x] == " ":
                    line.append(_DIR_GLYPH.get(directions, "·"), style="dim")
                else:
                    line.append(self._ch[y][x], style=self._st[y][x] or None)
            lines.append(line)
        return lines


def _body_text(node: _DagNode, row: RowLike | None) -> str:
    if node.type == "decision":
        cond = node.condition or node.description
        if row is not None and row.branch_taken:
            return f"→{row.branch_taken} {cond}".strip()
        return f"? {cond}" if cond else "decision"
    if node.subplan_size is not None:
        size = f"({node.subplan_size} nodes)"
        return f"{node.description} {size}".strip() if node.description else size
    return node.description or node.tool or node.type


def _normalise_status(row: RowLike | None) -> str:
    """Map a row's status onto a glyph key; absent row -> pending.

    The view stores tool failures as ``"failed: <error>"``; collapse on the
    first colon so the glyph lookup still resolves.
    """
    if row is None:
        return "pending"
    return row.status.split(":", 1)[0].strip() or "pending"


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_nodes(plan: dict[str, Any]) -> list[_DagNode]:
    raw = plan.get("nodes")
    if not isinstance(raw, list):
        return []
    nodes: list[_DagNode] = []
    for raw_item in cast("list[Any]", raw):
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, Any]", raw_item)
        nid = item.get("id")
        if not isinstance(nid, int) or isinstance(nid, bool):
            continue
        nodes.append(
            _DagNode(
                id=nid,
                type=str(item.get("type", "")),
                description=str(item.get("description", "")),
                tool=_opt_str(item.get("tool")),
                context_needed=_int_tuple(item.get("context_needed")),
                condition=_opt_str(item.get("condition")),
                branch_targets=_branch_targets(item.get("branches")),
                subplan_size=_subplan_size(item.get("subplan")),
            )
        )
    return nodes


def _layers(nodes: list[_DagNode]) -> list[list[int]]:
    """Assign each node to a layer = longest path from any root.

    Edges are ``context_needed`` (data deps) plus decision→branch-target
    (control deps), so a gated node sinks below the decision that guards it.
    Plans are validated acyclic before they reach here; the ``visiting`` guard
    only keeps a malformed plan from recursing forever.
    """
    if not nodes:
        return []
    ids = {n.id for n in nodes}
    parents: dict[int, set[int]] = {n.id: set() for n in nodes}
    for node in nodes:
        for dep in node.context_needed:
            if dep in ids:
                parents[node.id].add(dep)
        for target in node.branch_targets:
            if target in ids:
                parents[target].add(node.id)

    depth: dict[int, int] = {}
    visiting: set[int] = set()

    def resolve(nid: int) -> int:
        if nid in depth:
            return depth[nid]
        if nid in visiting:
            return 0
        visiting.add(nid)
        ps = parents[nid]
        depth[nid] = 1 + max((resolve(p) for p in ps), default=-1)
        visiting.discard(nid)
        return depth[nid]

    for node in nodes:
        resolve(node.id)

    layers: list[list[int]] = [[] for _ in range(max(depth.values(), default=0) + 1)]
    for nid in sorted(depth):
        layers[depth[nid]].append(nid)
    return layers


def _subplan_size(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    nodes = cast("dict[str, Any]", value).get("nodes")
    return len(cast("list[Any]", nodes)) if isinstance(nodes, list) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int_tuple(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, int) and not isinstance(v, bool))


def _branch_targets(value: Any) -> tuple[int, ...]:
    if not isinstance(value, dict):
        return ()
    targets: list[int] = []
    for branch in cast("dict[str, Any]", value).values():
        targets.extend(_int_tuple(branch))
    return tuple(targets)
