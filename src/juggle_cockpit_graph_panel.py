"""Rich renderable builder for the cockpit task-graph panel.

Turns the pure layout (juggle_cockpit_graph_layout) + selection + unread badge
into a Rich Panel that swaps into the cockpit's Notifications widget when graph
mode is active. Rich-only: no DB, no Textual app. Read-only.

Layout is a multi-column NUMBERED LIST in topological (execution) order: one
task per cell — index, state glyph, task id, and a state suffix (running agent
label, or a dep-wait suffix for a blocked task). Cells flow column-major to fill a wide,
short pane so all tasks stay readable without a horizontal scroll. A power-user
view of a mostly-linear pipeline.
"""
from __future__ import annotations

import math

from rich.console import Group as _Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style

from juggle_cockpit_graph_layout import GraphTask, frontier_visible
from juggle_cockpit_legend import graph_inline_legend, TASK_STATE_GLYPHS

# Per-project row/section renderers extracted to juggle_cockpit_graph_rows
# (R1, 2026-06-30 graph railroad); re-imported so this module's public symbols
# (topological_order, _cell_text, …) and external importers keep resolving here.
from juggle_cockpit_graph_rows import (  # noqa: F401  (re-export)
    _CELL_WIDTH,
    _MAX_COLS,
    _STATE_COLORS,
    _RUNNING_STATES,
    _badge_segment,
    _progress_bar,
    _section_header,
    topological_order,
    _cell_text,
    _flat_selectable,
    _graph_section,
)


def build_graph_panel(
    *,
    project_id: str | None,
    tasks: list[GraphTask],
    edges: list[tuple[str, str]],
    selection: int,
    unread: int,
    width: int,
    height: int,
    pan_offset: int,
    project_name: str | None = None,
    scroll: bool = False,
) -> Panel:
    """Build the graph Panel. Pure — no I/O.

    selection indexes the flat topological task list (execution order).
    width/height are the panel's available inner dims (cells). When scroll is
    True the full grid is rendered (no vertical truncation) so an outer
    scrollable viewport can pan over it.
    """
    title = f"Graph{_badge_segment(unread)}"

    if not project_id:
        body = Text("no project selected", style=Style(dim=True))
        return Panel(body, title=title, border_style="grey50")

    if not tasks:
        label = f"{project_id} · {project_name}" if project_name else project_id
        body = Text(f"{label}: no graph tasks yet", style=Style(dim=True))
        return Panel(body, title=title, border_style="grey50")

    inner_w_hdr = max(8, width - 4)
    real_tasks = [n for n in tasks if not getattr(n, "is_mirror", False)]
    header = _section_header(project_id, project_name, real_tasks, inner_w_hdr, edges)

    flat = topological_order(tasks, edges)
    # Global numbering source: every visible cell keeps its original topological
    # index (1-based) via idx_of, so dep suffixes (⧗#12) stay pointed at real
    # indices even though the pruned view hides most verified tasks.
    idx_of = {n.id: i + 1 for i, n in enumerate(flat)}
    first_dep: dict[str, str] = {}
    for task_id, dep_id in edges:
        first_dep.setdefault(task_id, dep_id)

    # Frontier prune (panel default, 2026-07-01): render the non-verified
    # frontier + one hop of verified context; hidden verified tasks collapse to a
    # single dim summary cell. Selection iterates this VISIBLE list only.
    visible, hidden = frontier_visible(tasks, edges)

    inner_w = max(8, width - 4)
    idx_w = len(str(len(flat)))
    n_cols = max(1, min(_MAX_COLS, inner_w // _CELL_WIDTH))
    cell_w = max(10, inner_w // n_cols - 1)
    rows = math.ceil(len(visible) / n_cols)

    # Vertical scroll when the list is taller than the pane (header+legend = 2).
    # When an outer scrollable viewport is in play (scroll=True), render the
    # full grid and let the viewport pan over it instead of truncating here.
    avail_rows = max(1, height - 2)
    truncated = (not scroll) and rows > avail_rows
    if truncated:
        rows = avail_rows

    sel_id = visible[selection].id if 0 <= selection < len(visible) else None

    grid = Table.grid(padding=(0, 1))
    for _ in range(n_cols):
        grid.add_column(no_wrap=True, overflow="ellipsis", width=cell_w)

    shown = 0
    for r in range(rows):
        cells: list = []
        for c in range(n_cols):
            i = c * rows + r            # column-major fill
            if i < len(visible):
                task = visible[i]
                dep_id = first_dep.get(task.id)
                dep_num = idx_of.get(dep_id) if dep_id else None
                cells.append(
                    _cell_text(
                        idx_of.get(task.id, i + 1), task, idx_w=idx_w,
                        dep_num=dep_num, cell_w=cell_w,
                        selected=(task.id == sel_id),
                    )
                )
                shown += 1
            else:
                cells.append(Text(""))
        grid.add_row(*cells)

    parts: list = [header, grid]
    if truncated and shown < len(visible):
        parts.append(
            Text(f"  … +{len(visible) - shown} more", style=Style(dim=True, italic=True))
        )
    if hidden > 0:
        done_glyph = TASK_STATE_GLYPHS["verified"]
        parts.append(
            Text(f"  {done_glyph} {hidden} earlier hidden", style=Style(dim=True))
        )
    legend = Text(graph_inline_legend(), style=Style(dim=True))
    parts.append(legend)

    return Panel(_Group(*parts), title=title, border_style="cyan")


def build_multi_graph_panel(
    *,
    dags: list,
    selection: int,
    unread: int,
    width: int,
    height: int,
    pan_offset: int,
    scroll: bool = False,
) -> Panel:
    """Stacked multi-DAG panel: one titled topic-DAG section per armed project.

    selection indexes the concatenated flat selectable list across dags.
    """
    title = f"Graph{_badge_segment(unread)}"
    if not dags:
        body = Text("no projects with tasks yet", style=Style(dim=True))
        return Panel(body, title=title, border_style="grey50")
    if len(dags) == 1:
        d = dags[0]
        return build_graph_panel(
            project_id=d.project_id, tasks=d.tasks, edges=d.edges,
            selection=selection, unread=unread, width=width, height=height,
            pan_offset=pan_offset,
            project_name=getattr(d, "project_name", None), scroll=scroll,
        )
    inner_w = max(8, width - 4)
    # Selection iterates the VISIBLE (frontier-pruned) list only, concatenated
    # across the stacked dags — same order the sections render.
    flat_all = [n for d in dags for n in frontier_visible(d.tasks, d.edges)[0]]
    sel_id = flat_all[selection].id if 0 <= selection < len(flat_all) else None
    parts: list = []
    for i, d in enumerate(dags):
        if i:
            parts.append(Text("─" * inner_w, style=Style(dim=True)))
        parts.extend(_graph_section(d.project_id, d.tasks, d.edges, sel_id,
                                    inner_w, pan_offset,
                                    project_name=getattr(d, "project_name", None)))
    return Panel(_Group(*parts), title=title, border_style="cyan")
