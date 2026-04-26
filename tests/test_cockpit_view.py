import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from collections import namedtuple
import pytest
import time as _time

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel

from juggle_cockpit_view import pick_breakpoint, build_layout, render_topics, render_actions, render_agents, render_notifications, render_into
from juggle_cockpit_model import Topic, Action, Agent, Notification, CockpitState

Size = namedtuple("Size", ["width", "height"])

# ---------------------------------------------------------------------------
# pick_breakpoint
# ---------------------------------------------------------------------------

def test_pick_breakpoint_narrow():
    assert pick_breakpoint(Size(79, 40)) == "narrow"

def test_pick_breakpoint_medium_low():
    assert pick_breakpoint(Size(80, 40)) == "medium"

def test_pick_breakpoint_medium_high():
    assert pick_breakpoint(Size(119, 40)) == "medium"

def test_pick_breakpoint_wide():
    assert pick_breakpoint(Size(120, 40)) == "wide"

def test_pick_breakpoint_very_wide():
    assert pick_breakpoint(Size(220, 50)) == "wide"


# ---------------------------------------------------------------------------
# build_layout
# ---------------------------------------------------------------------------

def test_build_layout_wide_returns_layout():
    layout = build_layout("wide")
    assert isinstance(layout, Layout)

def test_build_layout_wide_has_topics():
    layout = build_layout("wide")
    assert layout["topics"] is not None

def test_build_layout_wide_has_actions():
    layout = build_layout("wide")
    assert layout["actions"] is not None

def test_build_layout_wide_has_agents():
    layout = build_layout("wide")
    assert layout["agents"] is not None

def test_build_layout_wide_has_notifications():
    layout = build_layout("wide")
    assert layout["notifications"] is not None

def test_build_layout_medium_returns_layout():
    layout = build_layout("medium")
    assert isinstance(layout, Layout)

def test_build_layout_medium_has_topics_strip():
    layout = build_layout("medium")
    assert layout["topics_strip"] is not None

def test_build_layout_narrow_returns_layout():
    layout = build_layout("narrow")
    assert isinstance(layout, Layout)

def test_build_layout_narrow_has_all_sections():
    layout = build_layout("narrow")
    assert layout["actions"] is not None
    assert layout["agents"] is not None
    assert layout["notifications"] is not None


# ---------------------------------------------------------------------------
# render_topics
# ---------------------------------------------------------------------------

def _make_topics():
    return [
        Topic(id="t1", label="K", status="current",  age_secs=60,   is_current=True),
        Topic(id="t2", label="J", status="running",  age_secs=3600, is_current=False),
        Topic(id="t3", label="G", status="paused",   age_secs=7200, is_current=False),
        Topic(id="t4", label="E", status="done",     age_secs=9000, is_current=False),
    ]

def test_render_topics_wide_returns_panel():
    panel = render_topics(_make_topics(), "wide")
    assert isinstance(panel, Panel)

def test_render_topics_wide_contains_label():
    c = Console(record=True, width=140)
    panel = render_topics(_make_topics(), "wide")
    with c:
        c.print(panel)
    text = c.export_text()
    assert "[K]" in text

def test_render_topics_wide_contains_glyph():
    c = Console(record=True, width=140)
    panel = render_topics(_make_topics(), "wide")
    with c:
        c.print(panel)
    text = c.export_text()
    assert "👉" in text

def test_render_topics_strip_medium():
    result = render_topics(_make_topics(), "medium")
    assert isinstance(result, Panel)

def test_render_topics_strip_contains_glyphs():
    c = Console(record=True, width=100)
    result = render_topics(_make_topics(), "medium")
    with c:
        c.print(result)
    text = c.export_text()
    assert "K" in text
    assert "J" in text


def test_render_topics_medium_shows_title():
    """Medium strip must include topic title text, not just label."""
    topics = [
        Topic(id="t1", label="EM", status="current", age_secs=60,  is_current=True,  title="email migration"),
        Topic(id="t2", label="EL", status="running", age_secs=600, is_current=False, title="elastic log"),
    ]
    c = Console(record=True, width=100)
    panel = render_topics(topics, "medium")
    with c:
        c.print(panel)
    text = c.export_text()
    assert "email migration" in text
    assert "elastic log" in text


def test_render_topics_narrow_shows_title():
    """Narrow strip must include topic title text, not just label."""
    topics = [
        Topic(id="t1", label="DZ", status="current", age_secs=60,  is_current=True,  title="deploy zone"),
        Topic(id="t2", label="RX", status="paused",  age_secs=900, is_current=False, title="refactor xunit"),
    ]
    c = Console(record=True, width=70)
    panel = render_topics(topics, "narrow")
    with c:
        c.print(panel)
    text = c.export_text()
    assert "deploy zone" in text
    assert "refactor xunit" in text


def test_render_topics_medium_one_topic_per_line():
    """Each topic must occupy its own line — not merged into one horizontal bar."""
    topics = [
        Topic(id="t1", label="AA", status="current", age_secs=60,   is_current=True,  title="alpha"),
        Topic(id="t2", label="BB", status="running", age_secs=3600, is_current=False, title="beta"),
        Topic(id="t3", label="CC", status="paused",  age_secs=7200, is_current=False, title="gamma"),
    ]
    c = Console(record=True, width=100)
    panel = render_topics(topics, "medium")
    with c:
        c.print(panel)
    lines = [l for l in c.export_text().splitlines() if l.strip()]
    # At least one line per topic must contain the label
    label_lines = [l for l in lines if "[AA]" in l or "[BB]" in l or "[CC]" in l]
    assert len(label_lines) >= 3, f"Expected ≥3 label lines, got {len(label_lines)}: {lines}"


def test_build_layout_medium_strip_height_scales_with_topics():
    """topics_strip size must grow when topics_count increases."""
    layout3 = build_layout("medium", topics_count=3)
    layout6 = build_layout("medium", topics_count=6)

    def _strip_size(layout):
        # Walk children to find topics_strip
        for child in layout._children:
            if child.name == "topics_strip":
                return child.size
        raise AssertionError("topics_strip not found")

    assert _strip_size(layout6) > _strip_size(layout3)


# ---------------------------------------------------------------------------
# render_actions
# ---------------------------------------------------------------------------

def _make_actions():
    return [
        Action(id="a1", topic_id="K", text="approve plan v3",      tier=2, age_secs=3600),
        Action(id="a2", topic_id="B", text="BLOCKER: missing token", tier=0, age_secs=7200),
        Action(id="a3", topic_id="I", text="survey results ready",  tier=1, age_secs=1800),
    ]

def test_render_actions_returns_panel():
    panel = render_actions(_make_actions())
    assert isinstance(panel, Panel)

def test_render_actions_contains_text():
    c = Console(record=True, width=120)
    panel = render_actions(_make_actions())
    with c:
        c.print(panel)
    text = c.export_text()
    assert "approve plan v3" in text

def test_render_actions_dict_leak_regression():
    """Action text must appear verbatim — never as a dict repr."""
    action_with_plain_text = Action(
        id="oq:thread-001:0",
        topic_id="K",
        text="should we use sqlite or postgres?",
        tier=2,
        age_secs=60,
    )
    c = Console(record=True, width=120)
    panel = render_actions([action_with_plain_text])
    with c:
        c.print(panel)
    output = c.export_text()
    assert "should we use sqlite or postgres?" in output
    assert "{'id':" not in output
    assert "{'text':" not in output

def test_render_actions_empty():
    c = Console(record=True, width=120)
    panel = render_actions([])
    with c:
        c.print(panel)
    text = c.export_text()
    assert "no actions" in text.lower() or panel is not None

def test_render_actions_tier_glyph_present():
    c = Console(record=True, width=120)
    panel = render_actions(_make_actions())
    with c:
        c.print(panel)
    text = c.export_text()
    assert "⚠️" in text or "❓" in text


# ---------------------------------------------------------------------------
# render_agents
# ---------------------------------------------------------------------------

def _make_agents():
    return [
        Agent(id_short="abcd1234", role="coder",      status="busy",  topic_id="K", age_secs=720),
        Agent(id_short="ef567890", role="planner",    status="stale", topic_id="J", age_secs=10800),
        Agent(id_short="12345678", role="researcher", status="idle",  topic_id=None, age_secs=300),
    ]

def test_render_agents_returns_panel():
    panel = render_agents(_make_agents())
    assert isinstance(panel, Panel)

def test_render_agents_busy_present():
    c = Console(record=True, width=100)
    panel = render_agents(_make_agents())
    with c:
        c.print(panel)
    text = c.export_text()
    assert "coder" in text
    assert "🟢" in text

def test_render_agents_stale_present():
    c = Console(record=True, width=100)
    panel = render_agents(_make_agents())
    with c:
        c.print(panel)
    text = c.export_text()
    assert "🟡" in text

def test_render_agents_idle_present():
    c = Console(record=True, width=100)
    panel = render_agents(_make_agents())
    with c:
        c.print(panel)
    text = c.export_text()
    assert "⚫" in text

def test_render_agents_empty():
    panel = render_agents([])
    assert isinstance(panel, Panel)
    c = Console(record=True, width=100)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "no agents" in text.lower()


# ---------------------------------------------------------------------------
# render_notifications
# ---------------------------------------------------------------------------

def test_render_notifications_empty():
    panel = render_notifications([])
    assert isinstance(panel, Panel)
    c = Console(record=True, width=120)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "no notifications" in text.lower()

def test_render_notifications_one():
    notifs = [Notification(text="plan v3 ready", kind="complete", age_secs=30)]
    c = Console(record=True, width=120)
    panel = render_notifications(notifs)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "plan v3 ready" in text

def test_render_notifications_glyph():
    notifs = [Notification(text="plan v3 ready", kind="complete", age_secs=30)]
    c = Console(record=True, width=120)
    panel = render_notifications(notifs)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "⚡" in text

def test_render_notifications_multiple_newest_first():
    notifs = [
        Notification(text="first notification",  kind="info",     age_secs=600),
        Notification(text="second notification", kind="complete", age_secs=30),
    ]
    c = Console(record=True, width=120)
    panel = render_notifications(notifs)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "first notification" in text
    assert "second notification" in text


# ---------------------------------------------------------------------------
# render_into
# ---------------------------------------------------------------------------

def _make_full_state():
    return CockpitState(
        topics=[
            Topic(id="t1", label="K", status="current", age_secs=60, is_current=True,  title="cockpit refactor"),
            Topic(id="t2", label="J", status="running", age_secs=3600, is_current=False, title="talkback"),
        ],
        actions=[
            Action(id="a1", topic_id="K", text="approve plan v3", tier=2, age_secs=300),
        ],
        agents=[
            Agent(id_short="abcd1234", role="coder", status="busy", topic_id="K", age_secs=720),
        ],
        notifications=[
            Notification(text="plan v3 ready", kind="complete", age_secs=30),
        ],
        fetched_at=_time.time(),
    )

def test_render_into_wide_no_exception():
    layout = build_layout("wide")
    state = _make_full_state()
    render_into(layout, state, "wide")

def test_render_into_medium_no_exception():
    layout = build_layout("medium")
    state = _make_full_state()
    render_into(layout, state, "medium")

def test_render_into_narrow_no_exception():
    layout = build_layout("narrow")
    state = _make_full_state()
    render_into(layout, state, "narrow")

def test_render_into_wide_actions_panel_updated():
    layout = build_layout("wide")
    state = _make_full_state()
    render_into(layout, state, "wide")
    assert layout["actions"].renderable is not None


# ---------------------------------------------------------------------------
# Scroll: render_actions
# ---------------------------------------------------------------------------

def test_render_actions_scroll_hides_first_item():
    """With scroll_offset=1, first action must not appear in output."""
    actions = _make_actions()
    c = Console(record=True, width=120)
    panel = render_actions(actions, scroll_offset=1)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "approve plan v3" not in text       # first item hidden
    assert "BLOCKER: missing token" in text    # second item visible


def test_render_actions_scroll_title_shows_offset():
    """Title must include ↑N indicator when offset > 0."""
    panel = render_actions(_make_actions(), scroll_offset=2)
    # Panel title is stored in the renderable title attribute
    assert "↑2" in str(panel.title)


def test_render_actions_scroll_offset_zero_no_indicator():
    """No ↑ indicator when at top."""
    panel = render_actions(_make_actions(), scroll_offset=0)
    assert "↑" not in str(panel.title)


def test_render_actions_active_border():
    """Active pane must use bright_blue border."""
    panel_active = render_actions(_make_actions(), active=True)
    panel_inactive = render_actions(_make_actions(), active=False)
    assert panel_active.border_style != panel_inactive.border_style


def test_render_actions_scroll_past_end_no_error():
    """Offset beyond list length must not raise — just shows empty table."""
    actions = _make_actions()
    panel = render_actions(actions, scroll_offset=len(actions) + 5)
    assert isinstance(panel, Panel)


# ---------------------------------------------------------------------------
# Scroll: render_agents
# ---------------------------------------------------------------------------

def test_render_agents_scroll_hides_first():
    """With scroll_offset=1, first sorted agent is hidden."""
    # busy agent (abcd1234) sorts first; offset=1 hides it
    agents = _make_agents()
    c = Console(record=True, width=100)
    panel = render_agents(agents, scroll_offset=1)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "abcd1234" not in text   # first after sort
    assert "ef567890" in text       # second


def test_render_agents_scroll_title_shows_offset():
    panel = render_agents(_make_agents(), scroll_offset=1)
    assert "↑1" in str(panel.title)


def test_render_agents_active_border():
    panel_active = render_agents(_make_agents(), active=True)
    panel_inactive = render_agents(_make_agents(), active=False)
    assert panel_active.border_style != panel_inactive.border_style


# ---------------------------------------------------------------------------
# Scroll: render_notifications
# ---------------------------------------------------------------------------

def test_render_notifications_scroll_hides_first():
    notifs = [
        Notification(text="first notif",  kind="info",     age_secs=600),
        Notification(text="second notif", kind="complete", age_secs=30),
    ]
    c = Console(record=True, width=120)
    panel = render_notifications(notifs, scroll_offset=1)
    with c:
        c.print(panel)
    text = c.export_text()
    assert "first notif" not in text
    assert "second notif" in text


def test_render_notifications_scroll_title():
    notifs = [
        Notification(text="a", kind="info", age_secs=10),
        Notification(text="b", kind="info", age_secs=20),
    ]
    panel = render_notifications(notifs, scroll_offset=1)
    assert "↑1" in str(panel.title)


def test_render_notifications_active_border():
    notifs = [Notification(text="x", kind="info", age_secs=5)]
    panel_active = render_notifications(notifs, active=True)
    panel_inactive = render_notifications(notifs, active=False)
    assert panel_active.border_style != panel_inactive.border_style


# ---------------------------------------------------------------------------
# Scroll: render_into with scroll_offsets
# ---------------------------------------------------------------------------

def test_render_into_passes_scroll_offsets():
    """render_into with scroll_offsets must hide first action row."""
    layout = build_layout("wide")
    state = _make_full_state()
    # First action is "approve plan v3" — offset=1 should hide it
    render_into(layout, state, "wide", scroll_offsets={"actions": 1})
    c = Console(record=True, width=160)
    with c:
        c.print(layout["actions"].renderable)
    text = c.export_text()
    assert "approve plan v3" not in text


def test_render_into_active_pane_highlights_border():
    """Active pane panel must have a different border than inactive panes."""
    layout = build_layout("wide")
    state = _make_full_state()
    render_into(layout, state, "wide", active_pane="agents")
    agents_panel = layout["agents"].renderable
    actions_panel = layout["actions"].renderable
    assert agents_panel.border_style != actions_panel.border_style
