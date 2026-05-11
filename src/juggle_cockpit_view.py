"""Juggle Cockpit View — dataclasses → Rich renderables. Zero I/O."""
from __future__ import annotations

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style

from juggle_cockpit_model import (
    Topic, Action, Agent, Notification, ScheduledTask, CockpitState, format_age
)
from juggle_settings import get_nested

# ---------------------------------------------------------------------------
# Glyph tables
# ---------------------------------------------------------------------------
TOPIC_STATUS_GLYPHS: dict[str, str] = {
    "current":    "👉",
    "running":    "🏃",
    "paused":     "⏸️",
    "done":       "✅",
    "closed":     "🔒",
    "failed":     "❌",
    "archived":   "🗄️",
    "active":     "🔵",
    "background": "🏃",
}

ACTION_TIER_GLYPHS: dict[int, str] = {
    0: "⚠️",   # blocker
    1: "📬",   # review ready
    2: "❓",   # open question
    3: "📝",   # nudge/note
}

AGENT_STATUS_GLYPHS: dict[str, str] = {
    "busy":  "🟢",
    "idle":  "⚫",
    "stale": "🟡",
}

SCHED_STATUS_GLYPHS: dict[str, str] = {
    "running": "🔄",
    "ok":      "✅",
    "failed":  "❌",
    "unknown": "⏸️",
}

NOTIF_KIND_GLYPHS: dict[str, str] = {
    "complete": "⚡",
    "info":     "ℹ️",
    "warning":  "⚠️",
    "error":    "✗",
    "failed":   "❌",
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
# Layout builder
# ---------------------------------------------------------------------------

def build_layout(bp: str, topics_count: int = 0) -> Layout:
    """Build a Rich Layout tree for the given breakpoint.

    Wide (>=120):
        root (horizontal)
        ├── topics   (ratio=t_ratio, full height)
        └── right    (ratio=a_ratio+ag_ratio, vertical)
            ├── upper (ratio=100-notif_ratio, horizontal)
            │   ├── actions (ratio=a_ratio)
            │   └── agents  (ratio=ag_ratio)
            └── notifications (ratio=notif_ratio)

    Medium (80-119):
        root (vertical)
        ├── upper (ratio=100-notif_ratio, horizontal)
        │   ├── actions (ratio=60)
        │   └── agents  (ratio=40)
        ├── topics_strip (size=topics_count+2, min 3)
        └── notifications (ratio=notif_ratio)

    Narrow (<80):
        root (vertical)
        ├── actions       (ratio=int((100-notif_ratio)*4/7))
        ├── agents        (ratio=(100-notif_ratio)-actions_ratio)
        ├── topics_strip  (size=topics_count+2, min 3)
        └── notifications (ratio=notif_ratio)
    """
    notif_ratio = get_nested("cockpit", "notification_ratio")
    col_ratios = get_nested("cockpit", "column_ratios") or [0.30, 0.40, 0.30]
    # col_ratios = [topics, actions, agents] — three visible columns
    t_ratio = int(col_ratios[0] * 100)
    a_ratio = int(col_ratios[1] * 100)
    ag_ratio = int(col_ratios[2] * 100)
    upper_ratio = 100 - notif_ratio
    strip_size = max(3, topics_count + 2)

    if bp == "wide":
        root = Layout(name="root")
        root.split_row(
            Layout(name="topics", ratio=t_ratio),
            Layout(name="right", ratio=a_ratio + ag_ratio),
        )
        root["right"].split_column(
            Layout(name="upper", ratio=upper_ratio),
            Layout(name="notifications", ratio=notif_ratio),
        )
        root["right"]["upper"].split_row(
            Layout(name="actions", ratio=a_ratio),
            Layout(name="agents", ratio=ag_ratio),
        )
        return root

    elif bp == "medium":
        root = Layout(name="root")
        root.split_column(
            Layout(name="upper", ratio=upper_ratio),
            Layout(name="topics_strip", size=strip_size),
            Layout(name="notifications", ratio=notif_ratio),
        )
        root["upper"].split_row(
            Layout(name="actions", ratio=60),
            Layout(name="agents", ratio=40),
        )
        return root

    else:  # narrow
        actions_ratio = int(upper_ratio * 4 / 7)
        agents_ratio = upper_ratio - actions_ratio
        root = Layout(name="root")
        root.split_column(
            Layout(name="actions", ratio=actions_ratio),
            Layout(name="agents", ratio=agents_ratio),
            Layout(name="topics_strip", size=strip_size),
            Layout(name="notifications", ratio=notif_ratio),
        )
        return root


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
        table.add_column("age",   no_wrap=True)
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
        return Panel(table, title="Action Items", border_style=border)

    visible = actions[scroll_offset:]
    table = Table.grid(padding=(0, 1))
    table.add_column("age",   no_wrap=True)
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

    return Panel(table, title=_scroll_title("Action Items", scroll_offset), border_style=border)


def render_agents(
    agents: list[Agent],
    scheduled: list[ScheduledTask] | None = None,
    scroll_offset: int = 0,
    active: bool = False,
) -> Panel:
    """Render agents panel. Single-line per agent: glyph + [label] + id_short + role + age.
    scroll_offset skips that many rows from the top; active highlights the border.
    """
    border = _pane_border(active)
    if not agents and not scheduled:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no agents", style=Style(dim=True)))
        return Panel(table, title="Agents", border_style=border)

    table = Table.grid(padding=(0, 1))
    table.add_column("glyph", no_wrap=True)
    table.add_column("topic", no_wrap=True)
    table.add_column("id",    no_wrap=True)
    table.add_column("role",  no_wrap=True)
    table.add_column("age",   no_wrap=True)

    # Sort: busy first, stale second, idle last; then slice for scroll
    _sort_order = {"busy": 0, "stale": 1, "idle": 2}
    sorted_agents = sorted(agents, key=lambda a: (_sort_order.get(a.status, 3), a.id_short))
    visible = sorted_agents[scroll_offset:]

    for agent in visible:
        glyph = AGENT_STATUS_GLYPHS.get(agent.status, "•")
        topic_str = f"[{agent.topic_id}]" if agent.topic_id else " — "
        age_str = format_age(agent.age_secs)

        if agent.status == "busy":
            row_style = Style(color="green")
        elif agent.status == "stale":
            row_style = Style(color="yellow")
        else:
            row_style = Style(dim=True)

        table.add_row(
            Text(glyph),
            Text(topic_str, style=row_style),
            Text(agent.id_short, style=row_style),
            Text(agent.role, style=row_style),
            Text(age_str, style=row_style),
        )

    if scheduled:
        def _fmt_schedule(s: str) -> str:
            return s.replace("every ", "").replace("daily ", "").replace("on-demand", "—").replace("on-change", "chg")

        def _trunc(s: str, n: int = 16) -> str:
            return s if len(s) <= n else s[:n - 1] + "…"

        table.add_row(Text(""), Text(""), Text("──"), Text("scheduled", style=Style(dim=True)), Text(""))
        for t in scheduled:
            glyph = SCHED_STATUS_GLYPHS.get(t.status, "•")
            label_style = Style(bold=True, color="red") if t.status == "failed" else Style(dim=True)
            table.add_row(
                Text(glyph),
                Text(""),
                Text(""),
                Text(_trunc(t.label), style=label_style),
                Text(_fmt_schedule(t.schedule), style=Style(dim=True)),
            )

    return Panel(table, title=_scroll_title("Agents", scroll_offset), border_style=border)


def render_notifications(
    notifications: list[Notification],
    scroll_offset: int = 0,
    active: bool = False,
) -> Panel:
    """Render notifications panel. Input is expected newest-first (from snapshot).
    scroll_offset skips that many rows from the top; active highlights the border.
    """
    border = _pane_border(active)
    if not notifications:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no notifications", style=Style(dim=True)))
        return Panel(table, title="Notifications", border_style=border)

    visible = notifications[scroll_offset:]
    table = Table.grid(padding=(0, 1))
    table.add_column("age",   no_wrap=True)
    table.add_column("glyph", no_wrap=True)
    table.add_column("text",  no_wrap=False, overflow="fold")

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

    return Panel(table, title=_scroll_title("Notifications", scroll_offset), border_style=border)


def render_into(
    layout: Layout,
    state: CockpitState | None,
    bp: str,
    scroll_offsets: dict[str, int] | None = None,
    active_pane: str | None = None,
) -> None:
    """Populate layout panels from state. Mutates layout in place.

    If state is None (DB error on first tick), renders placeholder panels.
    scroll_offsets maps pane name → row offset; active_pane highlights one border.
    """
    offsets = scroll_offsets or {}

    def _offset(pane: str) -> int:
        return offsets.get(pane, 0)

    def _active(pane: str) -> bool:
        return active_pane == pane

    if state is None:
        _placeholder = Panel(Text("loading…", style=Style(dim=True)))
        if bp == "wide":
            layout["topics"].update(_placeholder)
            layout["actions"].update(_placeholder)
            layout["agents"].update(_placeholder)
            layout["notifications"].update(_placeholder)
        else:
            layout["actions"].update(_placeholder)
            layout["agents"].update(_placeholder)
            layout["topics_strip"].update(_placeholder)
            layout["notifications"].update(_placeholder)
        return

    topics_panel  = render_topics(state.topics, bp)
    actions_panel = render_actions(state.actions, _offset("actions"), _active("actions"))
    agents_panel  = render_agents(state.agents, state.scheduled, _offset("agents"), _active("agents"))
    notifs_panel  = render_notifications(state.notifications, _offset("notifications"), _active("notifications"))

    if bp == "wide":
        layout["topics"].update(topics_panel)
        layout["actions"].update(actions_panel)
        layout["agents"].update(agents_panel)
        layout["notifications"].update(notifs_panel)
    elif bp == "medium":
        layout["actions"].update(actions_panel)
        layout["agents"].update(agents_panel)
        layout["topics_strip"].update(render_topics(state.topics, "medium"))
        layout["notifications"].update(notifs_panel)
    else:  # narrow
        layout["actions"].update(actions_panel)
        layout["agents"].update(agents_panel)
        layout["topics_strip"].update(render_topics(state.topics, "narrow"))
        layout["notifications"].update(notifs_panel)
