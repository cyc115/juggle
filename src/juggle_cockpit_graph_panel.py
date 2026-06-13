"""Rich renderable builder for the cockpit task-graph panel.

Turns the pure layout (juggle_cockpit_graph_layout) + selection + unread badge
into a Rich Panel that swaps into the cockpit's Notifications widget when graph
mode is active. Rich-only: no DB, no Textual app. Read-only.

Layout is a multi-column NUMBERED LIST in topological (execution) order: one
task per cell — index, state glyph, task id, and a state suffix (running agent
label, or ⊣<dep#> for a blocked task). Cells flow column-major to fill a wide,
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

from juggle_cockpit_graph_layout import GraphTask, build_ranks
from juggle_cockpit_view import TASK_STATE_GLYPHS
from juggle_graph_status import counts_from_states, format_progress

# Roughly the width one list cell wants: "10 🏃 cli-hooks-r8-guard [WK]".
_CELL_WIDTH = 26
_MAX_COLS = 4

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

_RUNNING_STATES = ("running", "dispatching", "integrating")


def _badge_segment(unread: int) -> str:
    return f" · ⚠{unread}" if unread > 0 else ""


def _progress_bar(tasks: list[GraphTask], width: int = 10) -> str:
    """Tiny ▕█░▏ done-fraction bar."""
    total = len(tasks) or 1
    done = sum(1 for n in tasks if n.state == "verified")
    filled = round(width * done / total)
    return "▕" + "█" * filled + "░" * (width - filled) + "▏"


def topological_order(
    tasks: list[GraphTask], edges: list[tuple[str, str]]
) -> list[GraphTask]:
    """Flatten the DAG into execution order: rank-major, id-stable within a rank.

    This is the display AND selection order, so j/k navigation matches the list.
    """
    flat: list[GraphTask] = []
    for rank in build_ranks(tasks, edges):
        flat.extend(rank.tasks)
    return flat


def _cell_text(
    idx: int,
    task: GraphTask,
    *,
    idx_w: int,
    dep_num: int | None,
    cell_w: int,
    selected: bool,
) -> Text:
    """Render one list cell: '<idx> <glyph> <id><suffix>', truncated to cell_w."""
    glyph = TASK_STATE_GLYPHS.get(task.state, "⬢")
    if task.state in _RUNNING_STATES and (task.user_label or task.thread_id):
        suffix = f" [{task.user_label or task.thread_id[:4]}]"
    elif task.state == "pending" and dep_num is not None:
        suffix = f" ⊣{dep_num}"          # waiting on task #dep_num
    elif task.state == "ready":
        suffix = " ▸"                     # next up
    elif task.state.startswith("failed") or task.state == "blocked-failed":
        suffix = " ✗"
    else:
        suffix = ""
    if getattr(task, "tasks_total", None):
        suffix = f" {task.tasks_done}/{task.tasks_total}" + suffix

    prefix = f"{idx:>{idx_w}} {glyph} "
    budget = max(3, cell_w - len(prefix) - len(suffix))
    name = task.id if len(task.id) <= budget else task.id[: max(1, budget - 1)] + "…"
    label = f"{prefix}{name}{suffix}"
    style = Style(
        color=_STATE_COLORS.get(task.state, "white"),
        bold=(task.state in _RUNNING_STATES),
        reverse=selected,
    )
    return Text(label, style=style, no_wrap=True, overflow="ellipsis")


def _flat_selectable(tasks: list[GraphTask]) -> list[GraphTask]:
    """Topological order for a DAG — shared by panel and multi-panel."""
    return topological_order(tasks, [])


def _graph_section(
    project_id: str,
    tasks: list[GraphTask],
    edges: list[tuple[str, str]],
    sel_id: str | None,
    inner_w: int,
    pan_offset: int,
) -> list:
    """Header + grid (no Panel wrapper) — extracted so build_multi_graph_panel
    can stack multiple sections."""
    if not tasks:
        return [Text(f"{project_id}: no graph tasks yet", style=Style(dim=True))]

    counts = counts_from_states([n.state for n in tasks])
    header = Text(
        f"{project_id}  {_progress_bar(tasks)}  {format_progress(counts)}",
        style=Style(bold=True),
    )

    flat = topological_order(tasks, edges)
    idx_of = {n.id: i + 1 for i, n in enumerate(flat)}
    first_dep: dict[str, str] = {}
    for task_id, dep_id in edges:
        first_dep.setdefault(task_id, dep_id)

    idx_w = len(str(len(flat)))
    n_cols = max(1, min(_MAX_COLS, inner_w // _CELL_WIDTH))
    cell_w = max(10, inner_w // n_cols - 1)
    rows = math.ceil(len(flat) / n_cols)

    grid = Table.grid(padding=(0, 1))
    for _ in range(n_cols):
        grid.add_column(no_wrap=True, overflow="ellipsis", width=cell_w)

    for r in range(rows):
        cells: list = []
        for c in range(n_cols):
            i = c * rows + r
            if i < len(flat):
                task = flat[i]
                dep_id = first_dep.get(task.id)
                dep_num = idx_of.get(dep_id) if dep_id else None
                cells.append(
                    _cell_text(
                        i + 1, task, idx_w=idx_w, dep_num=dep_num,
                        cell_w=cell_w, selected=(task.id == sel_id),
                    )
                )
            else:
                cells.append(Text(""))
        grid.add_row(*cells)

    return [header, grid]


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
) -> Panel:
    """Build the graph Panel. Pure — no I/O.

    selection indexes the flat topological task list (execution order).
    width/height are the panel's available inner dims (cells).
    """
    title = f"Graph{_badge_segment(unread)}"

    if not project_id:
        body = Text(
            "no armed graph — arm a project with /juggle:toggle-autopilot",
            style=Style(dim=True),
        )
        return Panel(body, title=title, border_style="grey50")

    if not tasks:
        body = Text(f"{project_id}: no graph tasks yet", style=Style(dim=True))
        return Panel(body, title=title, border_style="grey50")

    counts = counts_from_states([n.state for n in tasks])
    header = Text(
        f"{project_id}  {_progress_bar(tasks)}  {format_progress(counts)}",
        style=Style(bold=True),
    )

    flat = topological_order(tasks, edges)
    idx_of = {n.id: i + 1 for i, n in enumerate(flat)}
    first_dep: dict[str, str] = {}
    for task_id, dep_id in edges:
        first_dep.setdefault(task_id, dep_id)

    inner_w = max(8, width - 4)
    idx_w = len(str(len(flat)))
    n_cols = max(1, min(_MAX_COLS, inner_w // _CELL_WIDTH))
    cell_w = max(10, inner_w // n_cols - 1)
    rows = math.ceil(len(flat) / n_cols)

    # Vertical scroll when the list is taller than the pane (header+legend = 2).
    avail_rows = max(1, height - 2)
    truncated = rows > avail_rows
    if truncated:
        rows = avail_rows

    sel_id = flat[selection].id if 0 <= selection < len(flat) else None

    grid = Table.grid(padding=(0, 1))
    for _ in range(n_cols):
        grid.add_column(no_wrap=True, overflow="ellipsis", width=cell_w)

    shown = 0
    for r in range(rows):
        cells: list = []
        for c in range(n_cols):
            i = c * rows + r            # column-major fill
            if i < len(flat):
                task = flat[i]
                dep_id = first_dep.get(task.id)
                dep_num = idx_of.get(dep_id) if dep_id else None
                cells.append(
                    _cell_text(
                        i + 1, task, idx_w=idx_w, dep_num=dep_num,
                        cell_w=cell_w, selected=(task.id == sel_id),
                    )
                )
                shown += 1
            else:
                cells.append(Text(""))
        grid.add_row(*cells)

    parts: list = [header, grid]
    if truncated and shown < len(flat):
        parts.append(
            Text(f"  … +{len(flat) - shown} more", style=Style(dim=True, italic=True))
        )
    legend = Text(
        "🏃 running  ◇ ready  ⬡ blocked  ✅ done  ❌ failed   ⊣n=waits on #n",
        style=Style(dim=True),
    )
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
) -> Panel:
    """Stacked multi-DAG panel: one titled topic-DAG section per armed project.

    selection indexes the concatenated flat selectable list across dags.
    """
    title = f"Graph{_badge_segment(unread)}"
    if not dags:
        body = Text(
            "no armed graph — arm a project with /juggle:toggle-autopilot",
            style=Style(dim=True),
        )
        return Panel(body, title=title, border_style="grey50")
    if len(dags) == 1:
        d = dags[0]
        return build_graph_panel(
            project_id=d.project_id, tasks=d.tasks, edges=d.edges,
            selection=selection, unread=unread, width=width, height=height,
            pan_offset=pan_offset,
        )
    inner_w = max(8, width - 4)
    flat_all = [n for d in dags for n in topological_order(d.tasks, d.edges)]
    sel_id = flat_all[selection].id if 0 <= selection < len(flat_all) else None
    parts: list = []
    for i, d in enumerate(dags):
        if i:
            parts.append(Text("─" * inner_w, style=Style(dim=True)))
        parts.extend(_graph_section(d.project_id, d.tasks, d.edges, sel_id,
                                    inner_w, pan_offset))
    return Panel(_Group(*parts), title=title, border_style="cyan")
