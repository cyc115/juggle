"""Juggle Cockpit View — dataclasses → Rich renderables. Zero I/O."""
from __future__ import annotations

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style

from juggle_cockpit_model import (
    Topic, Action, Agent, Notification, CockpitState, format_age
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

def build_layout(bp: str) -> Layout:
    """Build a Rich Layout tree for the given breakpoint.

    Wide (>=120):
        root (horizontal)
        ├── topics   (ratio=30, full height)
        └── right    (ratio=70, vertical)
            ├── upper (ratio=70, horizontal)
            │   ├── actions (ratio=50)
            │   └── agents  (ratio=50)
            └── notifications (ratio=30)

    Medium (80-119):
        root (vertical)
        ├── upper (ratio=60, horizontal)
        │   ├── actions (ratio=60)
        │   └── agents  (ratio=40)
        ├── topics_strip (size=3)
        └── notifications (ratio=20)

    Narrow (<80):
        root (vertical)
        ├── actions       (ratio=40)
        ├── agents        (ratio=30)
        ├── topics_strip  (size=3)
        └── notifications (ratio=30)
    """
    notif_ratio = get_nested("cockpit", "notification_ratio")
    col_ratios = get_nested("cockpit", "column_ratios") or [0.30, 0.40, 0.30]

    if bp == "wide":
        root = Layout(name="root")
        root.split_row(
            Layout(name="topics", ratio=int(col_ratios[0] * 100)),
            Layout(name="right", ratio=int(col_ratios[1] * 100)),
        )
        root["right"].split_column(
            Layout(name="upper", ratio=70),
            Layout(name="notifications", ratio=notif_ratio),
        )
        root["right"]["upper"].split_row(
            Layout(name="actions", ratio=50),
            Layout(name="agents", ratio=50),
        )
        return root

    elif bp == "medium":
        root = Layout(name="root")
        root.split_column(
            Layout(name="upper", ratio=60),
            Layout(name="topics_strip", size=3),
            Layout(name="notifications", ratio=notif_ratio),
        )
        root["upper"].split_row(
            Layout(name="actions", ratio=60),
            Layout(name="agents", ratio=40),
        )
        return root

    else:  # narrow
        root = Layout(name="root")
        root.split_column(
            Layout(name="actions", ratio=40),
            Layout(name="agents", ratio=30),
            Layout(name="topics_strip", size=3),
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
            elif t.status in ("done", "archived"):
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
        # Strip mode for medium and narrow
        parts = []
        for t in topics:
            glyph = TOPIC_STATUS_GLYPHS.get(t.status, "•")
            parts.append(f"{glyph}{t.label}")
        strip_text = Text("  ".join(parts))
        return Panel(strip_text, title="Topics", border_style="dim", height=3)


def render_actions(actions: list[Action]) -> Panel:
    """Render actions panel.

    Actions are expected pre-sorted (tier asc, age desc) from snapshot().
    Reads action.text (typed str field) — dict-repr leak structurally impossible.
    """
    if not actions:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no actions", style=Style(dim=True, color="green")))
        return Panel(table, title="Action Items", border_style="dim")

    table = Table.grid(padding=(0, 1))
    table.add_column("age",   no_wrap=True)
    table.add_column("glyph", no_wrap=True)
    table.add_column("topic", no_wrap=True)
    table.add_column("text", no_wrap=False, overflow="fold")

    for action in actions:
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

    return Panel(table, title="Action Items", border_style="dim")


def render_agents(agents: list[Agent]) -> Panel:
    """Render agents panel. Single-line per agent: glyph + [label] + id_short + role + age."""
    if not agents:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no agents", style=Style(dim=True)))
        return Panel(table, title="Agents", border_style="dim")

    table = Table.grid(padding=(0, 1))
    table.add_column("glyph", no_wrap=True)
    table.add_column("topic", no_wrap=True)
    table.add_column("id",    no_wrap=True)
    table.add_column("role",  no_wrap=True)
    table.add_column("age",   no_wrap=True)

    # Sort: busy first, stale second, idle last
    _sort_order = {"busy": 0, "stale": 1, "idle": 2}
    sorted_agents = sorted(agents, key=lambda a: (_sort_order.get(a.status, 3), a.id_short))

    for agent in sorted_agents:
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

    return Panel(table, title="Agents", border_style="dim")


def render_notifications(notifications: list[Notification]) -> Panel:
    """Render notifications panel. Input is expected newest-first (from snapshot)."""
    if not notifications:
        table = Table.grid()
        table.add_column()
        table.add_row(Text("no notifications", style=Style(dim=True)))
        return Panel(table, title="Notifications", border_style="dim")

    table = Table.grid(padding=(0, 1))
    table.add_column("age",   no_wrap=True)
    table.add_column("glyph", no_wrap=True)
    table.add_column("text",  no_wrap=False, overflow="fold")

    for notif in notifications:
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

    return Panel(table, title="Notifications", border_style="dim")


def render_into(layout: Layout, state: CockpitState | None, bp: str) -> None:
    """Populate layout panels from state. Mutates layout in place.

    If state is None (DB error on first tick), renders placeholder panels.
    """
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
    actions_panel = render_actions(state.actions)
    agents_panel  = render_agents(state.agents)
    notifs_panel  = render_notifications(state.notifications)

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
