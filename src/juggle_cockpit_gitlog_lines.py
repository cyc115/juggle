"""juggle_cockpit_gitlog_lines — PURE full-screen git-log line model
(cockpit-gitlog-view, part 2 of the 2026-06-30 graph railroad; depends on
cockpit-frontier-prune). Turns a list of GraphDag into one GitLogRow per node,
newest/active-first, with a rounded-corner (╮╯╭╰│─) lane spine on the left
(via the shared ``assign_lanes`` engine) and the four approved metadata
columns (finish-time, sha, duration, topic-label/agent·model). No Textual, no
DB — ``juggle_cockpit_gitlog_meta.gitlog_meta_for`` supplies the per-id
GitLogMeta the DB-aware caller looks up; a missing/absent entry renders '—'.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from rich.text import Text
from rich.style import Style

from juggle_cockpit_graph_layout import GraphTask
from juggle_cockpit_graph_lanes import assign_lanes
from juggle_cockpit_graph_rows import topological_order, _STATE_COLORS
from juggle_cockpit_gitlog_meta import GitLogMeta
from juggle_cockpit_legend import (
    railroad_glyph,
    GRAPH_READY_SUFFIX,
    GRAPH_DEP_SUFFIX,
    GRAPH_FAIL_SUFFIX,
)

_RUNNING_STATES = ("running", "dispatching", "integrating")
_FAILED_STATES = ("failed-exec", "failed-integration", "failed-verify", "blocked-failed")
_EMPTY_META = GitLogMeta()


@dataclass(frozen=True)
class GitLogRow:
    row: int
    rail: str            # lane-gutter string, width = lane_count, glyph embedded
    idx: int              # 1-based, per-project topological index (matches graph panel)
    id: str
    title: str
    state: str
    meta: str              # formatted metadata segment (state word + columns)
    project_id: str
    selected: bool


def _leftmost_free(lanes: list) -> int:
    for i, h in enumerate(lanes):
        if h is None:
            return i
    lanes.append(None)
    return len(lanes) - 1


def _replay(order, edges):
    """Replay assign_lanes' packing (same algorithm as railroad_lines._replay)
    to capture, per row, lane occupancy plus the heading (merge) / branched
    (fan-out) lane indices."""
    ids = {n.id for n in order}
    row_of = {n.id: i for i, n in enumerate(order)}
    dependents: dict[str, list[str]] = {n.id: [] for n in order}
    for node_id, dep_id in edges:
        if node_id in ids and dep_id in ids:
            dependents[dep_id].append(node_id)
    for k in dependents:
        dependents[k].sort(key=lambda v: (row_of[v], v))

    lanes: list = []
    rows: list[dict] = []
    for n in order:
        nid = n.id
        before = [i for i, h in enumerate(lanes) if h is not None]
        heading = [i for i, h in enumerate(lanes) if h == nid]
        if heading:
            lane = heading[0]
            for i in heading[1:]:
                lanes[i] = None
        else:
            lane = _leftmost_free(lanes)
        lanes[lane] = None
        deps_v = dependents[nid]
        branched: list[int] = []
        if deps_v:
            lanes[lane] = deps_v[0]
            for v in deps_v[1:]:
                fl = _leftmost_free(lanes)
                lanes[fl] = v
                branched.append(fl)
        after = [i for i, h in enumerate(lanes) if h is not None]
        rows.append({
            "lane": lane, "heading": heading, "branched": branched,
            "before": before, "after": after,
        })
    return rows


def _rail_str(info: dict, glyph: str, width: int) -> str:
    """Lane-gutter string for one row — the node's own glyph sits AT its lane
    column; rounded corners (╮╭ fan-out, ╯╰ fan-in) replace the square
    railroad connectors so the view reads like ``git log --graph``."""
    lane = info["lane"]
    cols = [" "] * width
    for i in set(info["before"]) | set(info["after"]):
        if 0 <= i < width and i != lane:
            cols[i] = "│"
    branched = info["branched"]
    heading = info["heading"]
    if branched:
        cols[lane] = glyph
        span = [lane] + branched
        for c in range(lane + 1, max(span)):
            if cols[c] == " ":
                cols[c] = "─"
        for b in branched:
            cols[b] = "╮" if b > lane else "╭"
    elif len(heading) > 1:
        cols[lane] = glyph
        span = heading
        for c in range(lane + 1, max(span)):
            if cols[c] == " ":
                cols[c] = "─"
        for h in heading:
            if h != lane:
                cols[h] = "╯" if h > lane else "╰"
    else:
        cols[lane] = glyph
    return "".join(cols)


def _hhmm(iso: "str | None") -> "str | None":
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def _span(start_iso: "str | None", end_iso: "str | None") -> "str | None":
    """Human duration between two ISO timestamps, e.g. '45s', '12m', '1h05m'."""
    if not start_iso or not end_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    secs = int((end - start).total_seconds())
    if secs < 0:
        return None
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def _dep_num(task_id: str, first_dep: dict, idx_of: dict) -> "int | None":
    dep_id = first_dep.get(task_id)
    return idx_of.get(dep_id) if dep_id else None


def _meta_text(task: GraphTask, meta: GitLogMeta, dep_num: "int | None", *, now: "datetime | None") -> str:
    """Format the metadata segment following the approved column rules:
    running/dispatching/integrating -> elapsed + [topic-label] + agent·model;
    ready -> ready marker; open -> dep-wait marker; else -> finish-time + sha
    (+ duration when both timestamps are known)."""
    state = task.state
    if state in _RUNNING_STATES:
        now = now or datetime.now(timezone.utc)
        elapsed = _span(meta.dispatched_at, now.isoformat()) or "—"
        label = f"[{task.user_label}] " if task.user_label else ""
        agent_model = (
            f"{meta.role}·{meta.model}" if meta.role or meta.model else ""
        )
        return f"{state}  {label}{elapsed}  {agent_model}".rstrip()
    if state == "ready":
        return f"ready {GRAPH_READY_SUFFIX}"
    if state == "open":
        return f"open {GRAPH_DEP_SUFFIX}{dep_num}" if dep_num is not None else "open"
    # verified / failed-* / blocked-failed: finish-time + sha (+ duration)
    finish = _hhmm(meta.completed_at) or _hhmm(meta.verified_at) or "—"
    sha = meta.merged_sha[:7] if meta.merged_sha else "—"
    dur = _span(meta.dispatched_at, meta.completed_at)
    tail = f"  {sha}" + (f"  {dur}" if dur else "")
    suffix = f" {GRAPH_FAIL_SUFFIX}" if state in _FAILED_STATES else ""
    return f"{state}{suffix}  {finish}{tail}"


def gitlog_rows(
    dags: list,
    *,
    selected_row: int = 0,
    meta_by_id: "dict[str, GitLogMeta] | None" = None,
    now: "datetime | None" = None,
) -> list[GitLogRow]:
    """One GitLogRow per node across ALL dags (full history, unpruned),
    newest/active at the TOP within each project section."""
    meta_by_id = meta_by_id or {}
    out: list[GitLogRow] = []
    row_counter = 0
    for dag in dags:
        tasks = [n for n in dag.tasks if not getattr(n, "is_mirror", False)]
        if not tasks:
            continue
        order = topological_order(tasks, dag.edges)
        idx_of = {n.id: i + 1 for i, n in enumerate(order)}
        first_dep: dict[str, str] = {}
        for task_id, dep_id in dag.edges:
            first_dep.setdefault(task_id, dep_id)

        layout = assign_lanes(tasks, dag.edges)
        infos = _replay(order, getattr(layout, "edges", ()) or tuple(dag.edges))
        width = max(layout.lane_count, 1)

        section: list[GitLogRow] = []
        for task, info in zip(order, infos):
            glyph = railroad_glyph(task.state)
            meta = meta_by_id.get(task.id, _EMPTY_META)
            dep_num = _dep_num(task.id, first_dep, idx_of)
            section.append(GitLogRow(
                row=row_counter,
                rail=_rail_str(info, glyph, width),
                idx=idx_of[task.id],
                id=task.id,
                title=task.title,
                state=task.state,
                meta=_meta_text(task, meta, dep_num, now=now),
                project_id=dag.project_id,
                selected=False,
            ))
            row_counter += 1
        out.extend(reversed(section))  # newest/active at the top, per project

    if 0 <= selected_row < len(out):
        out[selected_row] = _with_selected(out[selected_row])
    return out


def _with_selected(row: GitLogRow) -> GitLogRow:
    return GitLogRow(
        row=row.row, rail=row.rail, idx=row.idx, id=row.id, title=row.title,
        state=row.state, meta=row.meta, project_id=row.project_id, selected=True,
    )


def render_gitlog_line(row: GitLogRow, *, idx_w: int = 3) -> Text:
    """One printable Rich Text line: rail (state-coloured) + idx + id + meta.
    Bold for the running family, dim for open — mirrors _cell_text's styling."""
    out = Text(no_wrap=True, overflow="ellipsis")
    rail_style = Style(
        color=_STATE_COLORS.get(row.state),
        bold=row.state in _RUNNING_STATES,
    )
    out.append(row.rail, style=rail_style)
    out.append(f" {row.idx:>{idx_w}} {row.id}  ", style=Style(bold=row.selected))
    out.append(row.meta, style=Style(dim=(row.state == "open")))
    if row.selected:
        out.stylize("reverse")
    return out
