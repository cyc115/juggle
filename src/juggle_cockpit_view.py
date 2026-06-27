"""Juggle Cockpit View — dataclasses → Rich renderables.

All functions are pure (no I/O). Static text renders (render_static /
render_static_from_state) live in juggle_cockpit_static.
"""

from __future__ import annotations

import re

from rich.console import Group as _RichGroup
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.style import Style

from juggle_cockpit_model import (
    Topic,
    Action,
    Agent,
    Notification,
    ScheduledTask,
    format_age,
    group_threads_by_project,
)

_LEADING_TAG_RE = re.compile(r"^\s*\[[^\]\n]+\]\s*")


def strip_leading_tag(title: str | None) -> str:
    """Drop ONE leading '[...]' tag + whitespace from a display title.

    Conversational titles have no leading bracket and are returned unchanged.
    If stripping would empty the string, the original is returned (never blank).
    """
    s = title or ""
    stripped = _LEADING_TAG_RE.sub("", s)
    return stripped if stripped else s


# ---------------------------------------------------------------------------
# Glyph tables
# ---------------------------------------------------------------------------
TOPIC_STATUS_GLYPHS: dict[str, str] = {
    "current": "👉",
    "running": "🏃",
    "paused": "⏸️",
    "done": "✅",
    "closed": "🔒",
    "failed": "❌",
    "archived": "🗄️",
    "active": "🔵",
    "background": "🏃",
}

ACTION_TIER_GLYPHS: dict[int, str] = {
    0: "⚠️",  # blocker
    1: "📬",  # review ready
    2: "❓",  # open question
    3: "📝",  # nudge/note
}

AGENT_STATUS_GLYPHS: dict[str, str] = {
    "busy": "🟢",
    "idle": "⚫",
    "stale": "🟡",
}

SCHED_STATUS_GLYPHS: dict[str, str] = {
    "running": "🔄",
    "ok": "✅",
    "failed": "❌",
    "unknown": "⏸️",
}

# Task-bound topics get their glyph from graph_tasks.state (autopilot, DA m2)
# — never from thread status/TTL, so done/failed tasks stay legible even after
# their threads close or archive.
TASK_STATE_GLYPHS: dict[str, str] = {
    "open": "⬡",
    "ready": "◇",
    "dispatching": "◌",
    "running": "🏃",
    "integrating": "🔀",
    "verified": "✅",
    "failed-exec": "❌",
    "failed-integration": "❌",
    "failed-verify": "❌",
    "blocked-failed": "🚫",
}

NOTIF_KIND_GLYPHS: dict[str, str] = {
    "complete": "⚡",
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "✗",
    "failed": "❌",
}


# ---------------------------------------------------------------------------
# Breakpoint picker
# ---------------------------------------------------------------------------


def pick_breakpoint(size_or_width) -> str:
    """Return 'wide', 'medium', or 'narrow' based on terminal width."""
    width = size_or_width if isinstance(size_or_width, int) else size_or_width.width
    if width >= 120:
        return "wide"
    if width >= 80:
        return "medium"
    return "narrow"


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


def _add_topic_row(table: Table, t: Topic, bp: str) -> None:
    task_state = getattr(t, "task_state", None)
    if task_state:
        glyph = TASK_STATE_GLYPHS.get(task_state, "⬢")
    else:
        glyph = TOPIC_STATUS_GLYPHS.get(t.status, "•")
    label_str = f"[{t.label}]"
    if t.is_current:
        style = Style(bold=True, color="white")
    elif t.status in ("done", "closed", "archived"):
        style = Style(dim=True)
    else:
        style = Style()
    # Single combined cell, RIGHT-cropped: age + glyph + [label] sit on the left
    # and are NEVER truncated; only the title ellipsizes on the right. bp no
    # longer changes the structure — the Topics pane is a fraction of the
    # terminal at any breakpoint, so uniform right-crop keeps the emoji safe.
    combined = Text(no_wrap=True, overflow="ellipsis")
    combined.append(f"{format_age(t.age_secs)} ", style=Style(dim=True))
    combined.append(glyph)
    combined.append(label_str, style=style)
    combined.append(" ")
    combined.append(strip_leading_tag(t.title) or t.label, style=style)
    table.add_row(combined)


def render_topics(
    topics: list[Topic],
    bp: str,
    projects_by_id: dict | None = None,
    scroll_offset: int = 0,
    active: bool = False,
    graph_by_project: dict | None = None,
) -> Panel:
    """Render topics panel.

    Wide: one row per topic with glyph + [label] + title.
    Medium/narrow: compressed strip.
    When projects_by_id has >1 project, renders section headers per project;
    a project with graph tasks shows aggregate progress in its header
    ('3/14 done, 1 failed, 2 ready' — autopilot, DA m2).
    """
    border = _pane_border(active)
    # Grouping also kicks in for a single project when graph counts exist —
    # aggregate row even with zero topics (DA round-2 minor 3, 2026-06-10).
    use_grouping = projects_by_id and (
        len(projects_by_id) > 1 or bool(graph_by_project)
    )
    visible = topics[scroll_offset:]

    def _make_table() -> Table:
        # One column holding the combined row Text; right-crop happens inside
        # the Text (overflow="ellipsis"), protecting age/glyph/[label] from
        # truncation at every breakpoint.
        t = Table.grid(padding=(0, 0))
        t.add_column("row", no_wrap=True, overflow="ellipsis")
        return t

    if not use_grouping:
        table = _make_table()
        for t in visible:
            _add_topic_row(table, t, bp)
        return Panel(table, title=_scroll_title("Topics", scroll_offset), border_style=border)

    # Grouped render — headers are separate Text renderables to avoid width=3 truncation
    groups = group_threads_by_project(visible, projects_by_id)
    # Synthesize a header row for graph projects with zero visible topics
    # (DA round-2 minor 3, 2026-06-10): an armed project whose tasks have no
    # live threads yet would otherwise vanish from the cockpit entirely.
    shown = {pid for pid, _, _ in groups}
    synthesized = [
        (pid, projects_by_id[pid], [])
        for pid in (graph_by_project or {})
        if pid not in shown and pid in projects_by_id
    ]
    if synthesized:
        inbox_at = next(
            (i for i, (pid, _, _) in enumerate(groups) if pid == "INBOX"),
            len(groups),
        )
        groups[inbox_at:inbox_at] = synthesized  # named projects before INBOX
    content_parts: list = []
    for idx, (project_id, project_name, group_topics) in enumerate(groups):
        if idx > 0:
            content_parts.append(Rule(style="dim"))
        hdr = Text()
        hdr.append(f" ▸ {project_name.upper()} ", style=Style(color="bright_white", bgcolor="grey23", bold=True))
        hdr.append(f" {len(group_topics)} ", style=Style(color="bright_white", bgcolor="grey23", dim=True))
        counts = (graph_by_project or {}).get(project_id)
        if counts:
            from juggle_graph_status import format_progress

            hdr.append(f" ⬢ {format_progress(counts)} ", style=Style(color="cyan", bgcolor="grey23"))
        content_parts.append(hdr)
        section_table = _make_table()
        for t in group_topics:
            _add_topic_row(section_table, t, bp)
        content_parts.append(section_table)
    return Panel(_RichGroup(*content_parts), title=_scroll_title("Topics", scroll_offset), border_style=border)


def _scroll_title(base: str, offset: int) -> str:
    return f"{base} [↑{offset}]" if offset > 0 else base


def _pane_border(active: bool) -> str:
    return "bright_blue" if active else "dim"


def render_actions(
    actions: list[Action],
    scroll_offset: int = 0,
    active: bool = False,
    filter_label: str = "",
) -> Panel:
    """Render actions panel.

    Actions are expected pre-sorted (tier asc, age desc) from snapshot().
    Reads action.text (typed str field) — dict-repr leak structurally impossible.
    scroll_offset skips that many rows from the top; active highlights the border.
    """
    border = _pane_border(active)
    if not actions:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no actions", style=Style(dim=True, color="green")))
        title = "Action Items"
        if filter_label:
            title = f"{title} [filter: {filter_label}]"
        return Panel(table, title=title, border_style=border)

    visible = actions[scroll_offset:]
    table = Table.grid(padding=(0, 0))
    table.add_column("age", no_wrap=True)
    table.add_column("glyph", no_wrap=True)
    table.add_column("topic", no_wrap=True)
    table.add_column("text", no_wrap=False, overflow="fold")

    for action in visible:
        glyph = ACTION_TIER_GLYPHS.get(action.tier, "•")
        topic_str = f"[{action.topic_id}]"

        if action.tier == 0:  # blocker
            text_style = Style(color="red", bold=True)
            topic_style = Style(color="red")
        elif action.tier == 1:  # review
            text_style = Style(color="yellow")
            topic_style = Style(color="yellow")
        else:
            text_style = Style()
            topic_style = Style(dim=True)

        table.add_row(
            Text(format_age(action.age_secs), style=Style(dim=True)),
            Text(glyph),
            Text(topic_str, style=topic_style),
            Text(action.text, style=text_style),  # action.text is always a str field
        )

    title = _scroll_title("Action Items", scroll_offset)
    if filter_label:
        title = f"{title} [filter: {filter_label}]"
    return Panel(table, title=title, border_style=border)


def render_agents(
    agents: list[Agent],
    scheduled: list[ScheduledTask] | None = None,
    scroll_offset: int = 0,
    active: bool = False,
    filter_label: str = "",
) -> Panel:
    """Render agents panel split into Active (topic-assigned) and Pool (idle/scheduled) sections."""
    border = _pane_border(active)
    if not agents and not scheduled:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no agents", style=Style(dim=True)))
        title = "Agents"
        if filter_label:
            title = f"{title} [filter: {filter_label}]"
        return Panel(table, title=title, border_style=border)

    _sort_order = {"busy": 0, "stale": 1, "idle": 2}
    sorted_agents = sorted(
        agents, key=lambda a: (_sort_order.get(a.status, 3), a.id_short)
    )
    visible = sorted_agents[scroll_offset:]

    active_agents = [a for a in visible if a.topic_id]
    pool_agents = [a for a in visible if not a.topic_id]

    def _row_style(status: str) -> Style:
        if status == "busy":
            return Style(color="green")
        if status == "stale":
            return Style(color="yellow")
        return Style(dim=True)

    parts: list = []

    # --- Active section: topic-assigned agents ---
    if active_agents:
        parts.append(Text("Active", style=Style(dim=True)))
        t_active = Table.grid(padding=(0, 0))
        t_active.add_column(no_wrap=True)  # #N
        t_active.add_column(no_wrap=True)  # glyph
        t_active.add_column(no_wrap=True)  # [topic]
        t_active.add_column(no_wrap=True)  # role
        t_active.add_column(no_wrap=True)  # age
        t_active.add_column(no_wrap=True)  # harness
        for agent in active_agents:
            i = sorted_agents.index(agent) + 1
            glyph = AGENT_STATUS_GLYPHS.get(agent.status, "•")
            st = _row_style(agent.status)
            hmodel = agent.harness or ""
            if agent.model:
                hmodel = f"{hmodel}/{agent.model}" if hmodel else agent.model
            t_active.add_row(
                Text(f"#{i}", style=Style(dim=True)),
                Text(glyph),
                Text(f"[{agent.topic_id}]", style=st),
                Text(f"(A) {agent.role}", style=st),
                Text(f" {format_age(agent.age_secs)}", style=st),
                Text(f" {hmodel}", style=Style(dim=True)),
            )
        parts.append(t_active)

    # --- Pool section: idle/unassigned agents + scheduled tasks ---
    if pool_agents or scheduled:
        if active_agents:
            parts.append(Text("─" * 22, style=Style(dim=True)))
        parts.append(Text("Pool", style=Style(dim=True)))
        if pool_agents:
            t_agents = Table.grid(padding=(0, 0))
            t_agents.add_column(no_wrap=True)  # glyph
            t_agents.add_column(no_wrap=True)  # role
            t_agents.add_column(no_wrap=True)  # age
            t_agents.add_column(no_wrap=True)  # harness
            for agent in pool_agents:
                glyph = AGENT_STATUS_GLYPHS.get(agent.status, "•")
                st = _row_style(agent.status)
                hmodel = agent.harness or ""
                if agent.model:
                    hmodel = f"{hmodel}/{agent.model}" if hmodel else agent.model
                t_agents.add_row(
                    Text(glyph),
                    Text(f"(A) {agent.role}", style=st),
                    Text(f" {format_age(agent.age_secs)}", style=st),
                    Text(f" {hmodel}", style=Style(dim=True)),
                )
            parts.append(t_agents)

        if scheduled:

            def _fmt_schedule(s: str) -> str:
                s = (
                    s.replace("every ", "")
                    .replace("daily ", "")
                    .replace("on-change", "chg")
                )
                return "" if s == "on-demand" else s

            def _trunc(s: str, n: int = 20) -> str:
                return s if len(s) <= n else s[: n - 1] + "…"

            t_sched = Table.grid(padding=(0, 0))
            t_sched.add_column(no_wrap=True)  # glyph
            t_sched.add_column(no_wrap=True)  # label
            t_sched.add_column(no_wrap=True)  # schedule
            for task in scheduled:
                glyph = SCHED_STATUS_GLYPHS.get(task.status, "⏰")
                label_style = (
                    Style(bold=True, color="red")
                    if task.status == "failed"
                    else Style(dim=True)
                )
                sched_str = _fmt_schedule(task.schedule)
                t_sched.add_row(
                    Text(glyph),
                    Text(f"(L) {_trunc(task.label)}", style=label_style),
                    Text(sched_str, style=Style(dim=True)),
                )
            parts.append(t_sched)

    title = _scroll_title("Agents", scroll_offset)
    if filter_label:
        title = f"{title} [filter: {filter_label}]"
    return Panel(_RichGroup(*parts), title=title, border_style=border)


def render_notifications(
    notifications: list[Notification],
    scroll_offset: int = 0,
    active: bool = False,
    filter_label: str = "",
) -> Panel:
    """Render notifications panel. Input is expected newest-first (from snapshot).
    scroll_offset skips that many rows from the top; active highlights the border.
    """
    border = _pane_border(active)
    if not notifications:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no notifications", style=Style(dim=True)))
        title = "Notifications"
        if filter_label:
            title = f"{title} [filter: {filter_label}]"
        return Panel(table, title=title, border_style=border)

    visible = notifications[scroll_offset:]
    table = Table.grid(padding=(0, 0))
    table.add_column("age", no_wrap=True)
    table.add_column("glyph", no_wrap=True)
    table.add_column("text", no_wrap=False, overflow="fold")

    for notif in visible:
        glyph = NOTIF_KIND_GLYPHS.get(notif.kind, "ℹ️")

        if notif.kind in ("error", "failed"):
            text_style = Style(color="red")
        elif notif.kind == "warning":
            text_style = Style(color="yellow")
        elif notif.kind == "complete":
            text_style = Style(color="green")
        else:
            text_style = Style()

        table.add_row(
            Text(format_age(notif.age_secs), style=Style(dim=True)),
            Text(glyph),
            Text(notif.text, style=text_style),
        )

    title = _scroll_title("Notifications", scroll_offset)
    if filter_label:
        title = f"{title} [filter: {filter_label}]"
    return Panel(table, title=title, border_style=border)


