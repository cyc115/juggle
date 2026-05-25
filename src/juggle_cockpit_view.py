"""Juggle Cockpit View — dataclasses → Rich renderables.

render_static_from_state / render_static are utility functions at the bottom
that do DB I/O; all other functions are pure (no I/O).
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style

from juggle_cockpit_model import (
    Topic,
    Action,
    Agent,
    Notification,
    ScheduledTask,
    CockpitState,
    format_age,
)
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


def pick_breakpoint(size) -> str:
    """Return 'wide', 'medium', or 'narrow' based on terminal width."""
    if size.width >= 120:
        return "wide"
    if size.width >= 80:
        return "medium"
    return "narrow"


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


def render_topics(topics: list[Topic], bp: str) -> Panel:
    """Render topics panel.

    Wide: one row per topic with glyph + [label] + title.
    Medium/narrow: compressed strip.
    """
    if bp == "wide":
        table = Table.grid(padding=(0, 1))
        table.add_column("age", no_wrap=True)
        table.add_column("glyph", no_wrap=True)
        table.add_column("label", no_wrap=True)
        table.add_column("title", no_wrap=False, overflow="fold")
        for t in topics:
            glyph = TOPIC_STATUS_GLYPHS.get(t.status, "•")
            label_str = f"[{t.label}]"
            if t.is_current:
                style = Style(bold=True, color="white")
            elif t.status in ("done", "closed", "archived"):
                style = Style(dim=True)
            else:
                style = Style()
            table.add_row(
                Text(format_age(t.age_secs), style=Style(dim=True)),
                Text(glyph),
                Text(label_str, style=style),
                Text(t.title or t.label, style=style),
            )
        return Panel(table, title="Topics", border_style="dim")

    else:
        # Vertical list for medium and narrow — one row per topic
        table = Table.grid(padding=(0, 1))
        table.add_column("glyph", no_wrap=True)
        table.add_column("label", no_wrap=True)
        table.add_column("title", no_wrap=False, overflow="fold")
        for t in topics:
            glyph = TOPIC_STATUS_GLYPHS.get(t.status, "•")
            label_str = f"[{t.label}]"
            if t.is_current:
                style = Style(bold=True, color="white")
            elif t.status in ("done", "closed", "archived"):
                style = Style(dim=True)
            else:
                style = Style()
            table.add_row(
                Text(glyph),
                Text(label_str, style=style),
                Text(t.title or t.label, style=style),
            )
        return Panel(table, title="Topics", border_style="dim")


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
    table = Table.grid(padding=(0, 1))
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
        t_active = Table.grid(padding=(0, 1))
        t_active.add_column(no_wrap=True)  # glyph
        t_active.add_column(no_wrap=True)  # [topic]
        t_active.add_column(no_wrap=True)  # role
        t_active.add_column(no_wrap=True)  # age
        for agent in active_agents:
            glyph = AGENT_STATUS_GLYPHS.get(agent.status, "•")
            st = _row_style(agent.status)
            t_active.add_row(
                Text(glyph),
                Text(f"[{agent.topic_id}]", style=st),
                Text(agent.role, style=st),
                Text(format_age(agent.age_secs), style=st),
            )
        parts.append(t_active)

    # --- Pool section: idle/unassigned agents + scheduled tasks ---
    if pool_agents or scheduled:
        if active_agents:
            parts.append(Text("─" * 22, style=Style(dim=True)))
        parts.append(Text("Pool", style=Style(dim=True)))
        t_pool = Table.grid(padding=(0, 1))
        t_pool.add_column(no_wrap=True)  # glyph
        t_pool.add_column(no_wrap=True)  # name
        t_pool.add_column(no_wrap=True)  # duration

        for agent in pool_agents:
            glyph = AGENT_STATUS_GLYPHS.get(agent.status, "•")
            st = _row_style(agent.status)
            t_pool.add_row(
                Text(glyph),
                Text(agent.role, style=st),
                Text(format_age(agent.age_secs), style=st),
            )

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

            for task in scheduled:
                glyph = SCHED_STATUS_GLYPHS.get(task.status, "⏰")
                label_style = (
                    Style(bold=True, color="red")
                    if task.status == "failed"
                    else Style(dim=True)
                )
                sched_str = _fmt_schedule(task.schedule)
                t_pool.add_row(
                    Text(glyph),
                    Text(_trunc(task.label), style=label_style),
                    Text(sched_str, style=Style(dim=True)),
                )

        parts.append(t_pool)

    title = _scroll_title("Agents", scroll_offset)
    if filter_label:
        title = f"{title} [filter: {filter_label}]"
    return Panel(Group(*parts), title=title, border_style=border)


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
    table = Table.grid(padding=(0, 1))
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


# ---------------------------------------------------------------------------
# Static render helpers (no Textual dependency — safe to import in test env)
# ---------------------------------------------------------------------------


def render_static_from_state(state: CockpitState, width: int = 120) -> str:
    """Render all four cockpit panes as plain text from a CockpitState.

    Mirrors the TUI 2D layout:
      Left column  : Topics (full height)
      Right top    : Actions + Agents side by side
      Right bottom : Notifications (full width of right)

    No DB I/O. Suitable for unit tests and CI smoke checks.
    """
    import io
    from rich.console import Console

    def _render(renderable, w: int) -> list[str]:
        """Render a Rich renderable into lines at width w without touching stdout."""
        buf = io.StringIO()
        con = Console(width=w, file=buf, no_color=True, highlight=False)
        con.print(renderable)
        return buf.getvalue().splitlines()

    left_w = width // 3
    right_w = width - left_w
    half_right = right_w // 2

    left_lines = _render(render_topics(state.topics, "wide"), left_w)
    actions_lines = _render(render_actions(state.actions), half_right)
    agents_lines = _render(render_agents(state.agents, state.scheduled), right_w - half_right)
    notif_lines = _render(render_notifications(state.notifications), right_w)

    # --- compose the 2D grid into lines ----------------------------------
    right_top_rows = max(len(actions_lines), len(agents_lines))
    total_rows = max(len(left_lines), right_top_rows + len(notif_lines))

    def _pad(lines: list[str], n: int, w: int) -> list[str]:
        padded = [ln.ljust(w)[:w] for ln in lines]
        padded += [" " * w] * (n - len(padded))
        return padded

    left_padded = _pad(left_lines, total_rows, left_w)
    actions_padded = _pad(actions_lines, right_top_rows, half_right)
    agents_padded = _pad(agents_lines, right_top_rows, right_w - half_right)
    notif_padded = _pad(notif_lines, len(notif_lines), right_w)

    right_padded: list[str] = []
    for i in range(right_top_rows):
        right_padded.append(actions_padded[i] + agents_padded[i])
    right_padded.extend(notif_padded)
    right_padded = _pad(right_padded, total_rows, right_w)

    output_lines = [l + r for l, r in zip(left_padded, right_padded)]
    return "\n".join(output_lines) + "\n"


def render_static(db_path: str | None = None, width: int = 120) -> str:
    """Snapshot the live juggle.db and render all four cockpit panes as plain text.

    Creates its own DB connection (does not reuse an existing one). Suitable for
    the ``--out`` CLI flag and CI health checks.
    """
    import sqlite3 as _sqlite3
    import sys as _sys
    from pathlib import Path as _Path

    _src = _Path(__file__).parent
    if str(_src) not in _sys.path:
        _sys.path.insert(0, str(_src))

    from juggle_cockpit_model import snapshot as _snapshot
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=db_path)
    db.init_db()
    conn = _sqlite3.connect(str(db.db_path))
    conn.row_factory = _sqlite3.Row
    db._connect = lambda: conn  # noqa: E731
    try:
        state = _snapshot(db)
        return render_static_from_state(state, width)
    finally:
        conn.close()
