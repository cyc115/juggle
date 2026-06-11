"""Rich renderable builder for the cockpit task-graph panel.

Turns the pure layout (juggle_cockpit_graph_layout) + selection + unread badge
into a Rich Panel that swaps into the cockpit's Notifications widget when graph
mode is active. Rich-only: no DB, no Textual app. Read-only.

Layout is left-to-right layered: each Rank is a column, arrows (→) flow
rightward between columns, narrow widths collapse foldable ranks to count cells
and pan via an horizontal window with a minimap bar.
"""
from __future__ import annotations

from rich.console import Group as _Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style

from juggle_cockpit_graph_layout import (
    GraphNode,
    Rank,
    build_ranks,
    collapse_ranks,
    minimap_bar,
    pan_window,
)
from juggle_cockpit_view import NODE_STATE_GLYPHS
from juggle_graph_status import counts_from_states, format_progress

# Each rank column needs roughly this many cells; used to pick how many ranks
# fit in the available width (cell = glyph + id + arrow gutter).
_RANK_COL_WIDTH = 12
_ARROW = "→"

_STATE_COLORS: dict[str, str] = {
    "verified": "green",
    "ready": "cyan",
    "running": "yellow",
    "dispatching": "yellow",
    "integrating": "yellow",
    "pending": "grey50",
    "failed-exec": "red",
    "failed-integration": "red",
    "failed-verify": "red",
    "blocked-failed": "red",
}


def _badge_segment(unread: int) -> str:
    return f" · ⚠{unread}" if unread > 0 else ""


def _cell_text(node: GraphNode, inner_w: int, selected: bool) -> Text:
    """Render one node cell: glyph + id (+ short thread label if room)."""
    glyph = NODE_STATE_GLYPHS.get(node.state, "⬢")
    label = f"{glyph} {node.id}"
    if node.thread_id and node.state in ("running", "dispatching", "integrating"):
        # Prefer the human-readable A-Z topic label so a running node
        # correlates with its row in the Agents pane; fall back to the
        # thread UUID prefix only if the thread row is gone.
        tag = node.user_label or node.thread_id[:4]
        extra = f" [{tag}]"
        if len(label) + len(extra) <= inner_w:
            label += extra
    if len(label) > inner_w:
        label = label[: max(1, inner_w - 1)] + "…"
    style = Style(color=_STATE_COLORS.get(node.state, "white"), bold=selected, reverse=selected)
    return Text(label, style=style)


def _rank_column(rank: Rank, inner_w: int, sel_id: str | None) -> _Group:
    """Render a single rank as a vertical stack of node cells (or a fold label)."""
    if rank.collapsed:
        return _Group(Text(rank.label[:inner_w], style=Style(dim=True, italic=True)))
    rows = [_cell_text(n, inner_w, selected=(n.id == sel_id)) for n in rank.nodes]
    return _Group(*rows) if rows else _Group(Text(""))


def _flat_selectable(nodes: list[GraphNode]) -> list[GraphNode]:
    """Selection order = rank order then id order (matches build_ranks)."""
    return sorted(nodes, key=lambda n: n.id)


def build_graph_panel(
    *,
    project_id: str | None,
    nodes: list[GraphNode],
    edges: list[tuple[str, str]],
    selection: int,
    unread: int,
    width: int,
    height: int,
    pan_offset: int,
) -> Panel:
    """Build the graph Panel. Pure — no I/O.

    selection indexes the flat selectable node list (rank-major, id order).
    width/height are the panel's available inner dims (cells).
    """
    title = f"Graph{_badge_segment(unread)}"

    if not project_id:
        body = Text("no armed graph — arm a project with /juggle:toggle-autopilot", style=Style(dim=True))
        return Panel(body, title=title, border_style="grey50")

    if not nodes:
        body = Text(f"{project_id}: no graph nodes yet", style=Style(dim=True))
        return Panel(body, title=title, border_style="grey50")

    # Header: progress via the shared formatter.
    counts = counts_from_states([n.state for n in nodes])
    header = Text(f"{project_id} · {format_progress(counts)}", style=Style(bold=True))

    # Layout → collapse to fit width → pan window.
    ranks = build_ranks(nodes, edges)
    inner_w = max(8, width - 4)
    max_ranks = max(1, inner_w // _RANK_COL_WIDTH)
    collapsed = collapse_ranks(ranks, max_visible_ranks=max_ranks)
    visible, mm = pan_window(collapsed, offset=pan_offset, visible_count=max_ranks)

    sel_nodes = _flat_selectable(nodes)
    sel_id = sel_nodes[selection].id if 0 <= selection < len(sel_nodes) else None

    # Build a horizontal grid: one column per visible rank, arrow gutter between.
    grid = Table.grid(padding=(0, 0))
    col_w = max(6, inner_w // max(1, len(visible)) - 2)
    n_cols = len(visible) * 2 - 1 if visible else 1
    for _ in range(n_cols):
        grid.add_column(no_wrap=True)
    cells: list = []
    for i, rank in enumerate(visible):
        cells.append(_rank_column(rank, col_w, sel_id))
        if i < len(visible) - 1:
            cells.append(Text(f" {_ARROW} ", style=Style(dim=True)))
    if cells:
        grid.add_row(*cells)

    parts: list = [header, grid]
    # Minimap only when panned/collapsed beyond the window.
    if mm.total > len(visible):
        parts.append(Text(minimap_bar(mm.total, mm.first, mm.last), style=Style(dim=True)))

    return Panel(_Group(*parts), title=title, border_style="cyan")
