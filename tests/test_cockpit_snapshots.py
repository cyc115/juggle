"""Unit tests for Juggle Cockpit v2 (Textual) — state logic and render reuse.

Tests cover:
- Splitter resize math (panel width clamping)
- Scroll offset state machine (clamp, cycle, scroll)
- v2 reuses v1 render functions (render_topics, render_actions, render_agents,
  render_notifications) — verified by importing from shared view module

Textual runtime (App.run_test) requires pytest-asyncio and is not run here;
these tests cover the pure-Python logic that is independent of the TUI event loop.
"""

import sys
import os
import time as _time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("rich")

from juggle_cockpit_model import (
    Action,
    Agent,
    CockpitState,
    Notification,
    ScheduledTask,
    Topic,
)
from juggle_cockpit_view import (
    render_actions,
    render_agents,
    render_notifications,
    render_topics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCROLL_PANES = ("actions", "agents", "notifications")


def _make_state():
    return CockpitState(
        topics=[
            Topic(id="t1", label="K", status="current", age_secs=300, is_current=True, title="cockpit v2"),
            Topic(id="t2", label="J", status="running", age_secs=3600, is_current=False, title="talkback"),
        ],
        actions=[
            Action(id="a1", topic_id="K", text="approve plan", tier=2, age_secs=60),
            Action(id="a2", topic_id="J", text="BLOCKER: missing token", tier=0, age_secs=300),
        ],
        agents=[
            Agent(id_short="abcd1234", role="coder", status="busy", topic_id="K", age_secs=120),
            Agent(id_short="ef567890", role="planner", status="idle", topic_id=None, age_secs=900),
        ],
        notifications=[
            Notification(text="plan ready", kind="complete", age_secs=30),
            Notification(text="agent timed out", kind="warning", age_secs=90),
        ],
        scheduled=[
            ScheduledTask(label="otter-daily", schedule="daily 19:00", status="ok", pid=None),
        ],
        fetched_at=_time.time(),
    )


# ---------------------------------------------------------------------------
# Splitter resize math
# ---------------------------------------------------------------------------


def _splitter_resize(left_start: int, right_start: int, delta: int, min_w: int = 8):
    """Replicate the Splitter.on_mouse_move width calculation."""
    total = left_start + right_start
    new_left = max(min_w, min(total - min_w, left_start + delta))
    new_right = total - new_left
    return new_left, new_right


class TestSplitterMath:
    def test_neutral_delta(self):
        left, right = _splitter_resize(30, 70, 0)
        assert left == 30
        assert right == 70

    def test_drag_right(self):
        left, right = _splitter_resize(30, 70, 10)
        assert left == 40
        assert right == 60
        assert left + right == 100

    def test_drag_left(self):
        left, right = _splitter_resize(30, 70, -10)
        assert left == 20
        assert right == 80

    def test_clamp_min_left(self):
        # drag so far left that left panel would go below minimum
        left, right = _splitter_resize(30, 70, -100)
        assert left == 8  # clamped to min_w
        assert right == 92

    def test_clamp_min_right(self):
        # drag so far right that right panel would go below minimum
        left, right = _splitter_resize(30, 70, 100)
        assert left == 92  # total - min_w
        assert right == 8

    def test_total_preserved(self):
        for delta in (-50, -10, 0, 10, 50):
            left, right = _splitter_resize(40, 60, delta)
            assert left + right == 100

    def test_custom_min_width(self):
        left, right = _splitter_resize(10, 10, -100, min_w=4)
        assert left == 4
        assert right == 16


# ---------------------------------------------------------------------------
# Scroll offset state machine
# ---------------------------------------------------------------------------


class _ScrollState:
    """Mirrors CockpitApp scroll logic for testability."""

    def __init__(self):
        self._offsets = {p: 0 for p in _SCROLL_PANES}
        self._active = "notifications"

    def scroll(self, delta: int):
        pane = self._active
        self._offsets[pane] = max(0, self._offsets[pane] + delta)

    def cycle(self):
        idx = _SCROLL_PANES.index(self._active) if self._active in _SCROLL_PANES else 0
        self._active = _SCROLL_PANES[(idx + 1) % len(_SCROLL_PANES)]

    def clamp(self, pane: str, max_val: int):
        self._offsets[pane] = min(self._offsets[pane], max_val)


class TestScrollState:
    def test_initial_state(self):
        s = _ScrollState()
        assert s._active == "notifications"
        assert s._offsets == {"actions": 0, "agents": 0, "notifications": 0}

    def test_scroll_down(self):
        s = _ScrollState()
        s.scroll(+3)
        assert s._offsets["notifications"] == 3

    def test_scroll_up_floor_at_zero(self):
        s = _ScrollState()
        s.scroll(-5)
        assert s._offsets["notifications"] == 0

    def test_scroll_only_affects_active_pane(self):
        s = _ScrollState()
        s.scroll(+5)
        assert s._offsets["actions"] == 0
        assert s._offsets["agents"] == 0
        assert s._offsets["notifications"] == 5

    def test_cycle_pane(self):
        s = _ScrollState()
        assert s._active == "notifications"
        s.cycle()
        assert s._active == "actions"
        s.cycle()
        assert s._active == "agents"
        s.cycle()
        assert s._active == "notifications"

    def test_scroll_after_cycle(self):
        s = _ScrollState()
        s.cycle()  # → actions
        s.scroll(+2)
        assert s._offsets["actions"] == 2
        assert s._offsets["agents"] == 0

    def test_clamp(self):
        s = _ScrollState()
        s.scroll(+10)
        s.clamp("notifications", 5)
        assert s._offsets["notifications"] == 5

    def test_clamp_below_current(self):
        s = _ScrollState()
        s.scroll(+3)
        s.clamp("notifications", 10)  # max > current — no change
        assert s._offsets["notifications"] == 3


# ---------------------------------------------------------------------------
# Render reuse: v2 uses the same view functions as v1
# ---------------------------------------------------------------------------


class TestRenderReuse:
    """Verify v1 render functions accept CockpitState data as used by v2."""

    def setup_method(self):
        self._state = _make_state()

    def test_render_topics_wide(self):
        panel = render_topics(self._state.topics, "wide")
        assert panel is not None
        assert panel.title == "Topics"

    def test_render_topics_medium(self):
        panel = render_topics(self._state.topics, "medium")
        assert panel is not None

    def test_render_actions_no_scroll(self):
        panel = render_actions(self._state.actions, scroll_offset=0, active=False)
        assert panel is not None
        assert "Action Items" in panel.title

    def test_render_actions_with_scroll(self):
        panel = render_actions(self._state.actions, scroll_offset=1, active=True)
        assert "↑1" in panel.title

    def test_render_agents(self):
        panel = render_agents(
            self._state.agents, self._state.scheduled, scroll_offset=0, active=False
        )
        assert panel is not None
        assert "Agents" in panel.title

    def test_render_notifications(self):
        panel = render_notifications(
            self._state.notifications, scroll_offset=0, active=False
        )
        assert panel is not None
        assert "Notifications" in panel.title

    def test_render_actions_empty(self):
        panel = render_actions([], scroll_offset=0, active=False)
        assert panel is not None

    def test_render_agents_empty(self):
        panel = render_agents([], [], scroll_offset=0, active=False)
        assert panel is not None


# ---------------------------------------------------------------------------
# Breakpoint routing
# ---------------------------------------------------------------------------


class TestBreakpointRouting:
    """pick_breakpoint used by both v1 and v2."""

    def test_wide(self):
        from collections import namedtuple
        from juggle_cockpit_view import pick_breakpoint
        Size = namedtuple("Size", ["width", "height"])
        assert pick_breakpoint(Size(140, 40)) == "wide"

    def test_medium(self):
        from collections import namedtuple
        from juggle_cockpit_view import pick_breakpoint
        Size = namedtuple("Size", ["width", "height"])
        assert pick_breakpoint(Size(100, 40)) == "medium"

    def test_narrow(self):
        from collections import namedtuple
        from juggle_cockpit_view import pick_breakpoint
        Size = namedtuple("Size", ["width", "height"])
        assert pick_breakpoint(Size(70, 40)) == "narrow"
