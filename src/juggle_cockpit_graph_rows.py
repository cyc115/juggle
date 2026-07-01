"""Per-project row/section renderers for the cockpit task-graph panel.

Extracted VERBATIM from juggle_cockpit_graph_panel (R1, 2026-06-30 graph railroad)
so the panel module stays within the LOC gate before the dependency spine swaps
in. Rich-only: no DB, no Textual. Behavior byte-identical to the pre-extraction
panel; juggle_cockpit_graph_panel re-imports these symbols.
"""
from __future__ import annotations

import math

from rich.table import Table
from rich.text import Text
from rich.style import Style

from juggle_cockpit_graph_layout import GraphTask, build_ranks
from juggle_cockpit_legend import (
    TASK_STATE_GLYPHS,
    GRAPH_READY_SUFFIX,
    GRAPH_DEP_SUFFIX,
    GRAPH_FAIL_SUFFIX,
    MIRROR_PREFIX,
    UNREAD_BADGE,
    FALLBACK_TASK,
)
from juggle_graph_status import counts_from_states, format_progress

# Roughly the width one list cell wants: "10 (running) cli-hooks-r8-guard [WK]".
_CELL_WIDTH = 26
_MAX_COLS = 4

_STATE_COLORS: dict[str, str] = {
    "verified": "green",
    "ready": "cyan",
    "running": "yellow",
    "dispatching": "yellow",
    "integrating": "yellow",
    "open": "grey50",
    "failed-exec": "red",
    "failed-integration": "red",
    "failed-verify": "red",
    "blocked-failed": "red",
}

_RUNNING_STATES = ("running", "dispatching", "integrating")


def _badge_segment(unread: int) -> str:
    return f" · {UNREAD_BADGE}{unread}" if unread > 0 else ""


def _progress_bar(tasks: list[GraphTask], width: int = 10) -> str:
    """Tiny ▕█░▏ done-fraction bar."""
    total = len(tasks) or 1
    done = sum(1 for n in tasks if n.state == "verified")
    filled = round(width * done / total)
    return "▕" + "█" * filled + "░" * (width - filled) + "▏"


def _section_header(
    project_id: str,
    project_name: str | None,
    real_tasks: list[GraphTask],
    inner_w: int,
) -> Text:
    """Header line: '<id> · <name>  <bar>  <progress>'.

    The id+name label is truncated with an ellipsis so the whole line fits
    inner_w, while the progress bar and done/running counts are kept intact.
    """
    counts = counts_from_states([n.state for n in real_tasks])
    suffix = f"  {_progress_bar(real_tasks)}  {format_progress(counts)}"
    label = f"{project_id} · {project_name}" if project_name else project_id
    budget = max(3, inner_w - len(suffix))
    if len(label) > budget:
        label = label[: max(1, budget - 1)] + "…"
    return Text(f"{label}{suffix}", style=Style(bold=True), no_wrap=True, overflow="ellipsis")


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
    """Render one list cell: '<idx> <glyph> [id] <name><suffix>', truncated to
    cell_w. The [thread/topic id] badge sits BEFORE the name (2026-06-16 user
    feedback) so it survives name truncation instead of being ellipsized off the
    end."""
    glyph = TASK_STATE_GLYPHS.get(task.state, FALLBACK_TASK)
    # Thread/topic id badge — rendered in front of the name so a long, truncated
    # name never hides it.
    if task.state in _RUNNING_STATES and (task.user_label or task.thread_id):
        id_seg = f"[{task.user_label or task.thread_id[:4]}] "
    else:
        id_seg = ""
    if task.state == "open" and dep_num is not None:
        suffix = f" {GRAPH_DEP_SUFFIX}{dep_num}"   # waiting on task #dep_num
    elif task.state == "ready":
        suffix = f" {GRAPH_READY_SUFFIX}"          # next up
    elif task.state.startswith("failed") or task.state == "blocked-failed":
        suffix = f" {GRAPH_FAIL_SUFFIX}"
    else:
        suffix = ""
    if getattr(task, "tasks_total", None):
        suffix = f" {task.tasks_done}/{task.tasks_total}" + suffix

    is_mirror = getattr(task, "is_mirror", False)
    prefix = f"{idx:>{idx_w}} {glyph} {id_seg}"
    budget = max(3, cell_w - len(prefix) - len(suffix))
    raw_name = task.id if len(task.id) <= budget else task.id[: max(1, budget - 1)] + "…"
    name = f"{MIRROR_PREFIX}{raw_name}" if is_mirror else raw_name
    label = f"{prefix}{name}{suffix}"
    style = Style(
        color="grey50" if is_mirror else _STATE_COLORS.get(task.state, "white"),
        dim=is_mirror,
        bold=(not is_mirror and task.state in _RUNNING_STATES),
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
    project_name: str | None = None,
) -> list:
    """Header + grid (no Panel wrapper) — extracted so build_multi_graph_panel
    can stack multiple sections."""
    if not tasks:
        label = f"{project_id} · {project_name}" if project_name else project_id
        return [Text(f"{label}: no graph tasks yet", style=Style(dim=True))]

    real_tasks = [n for n in tasks if not getattr(n, "is_mirror", False)]
    header = _section_header(project_id, project_name, real_tasks, inner_w)

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
