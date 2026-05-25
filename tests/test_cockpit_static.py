"""TDD tests for cockpit static render (--out mode).

render_static_from_state and render_static live in juggle_cockpit_view so they
can be imported without textual in the standard pytest env.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("rich")

from juggle_cockpit_model import (
    Action,
    Agent,
    CockpitState,
    Notification,
    Topic,
)
from juggle_cockpit_view import render_static_from_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    topics=None,
    actions=None,
    agents=None,
    notifications=None,
):
    return CockpitState(
        topics=topics or [],
        actions=actions or [],
        agents=agents or [],
        notifications=notifications or [],
        scheduled=[],
        fetched_at=time.time(),
    )


# ---------------------------------------------------------------------------
# render_static_from_state
# ---------------------------------------------------------------------------


def test_render_static_from_state_contains_pane_headers():
    """Output must include all four panel titles."""
    state = _make_state()
    text = render_static_from_state(state)
    assert "Topics" in text
    assert "Action Items" in text
    assert "Agents" in text
    assert "Notifications" in text


def test_render_static_from_state_shows_topic_label():
    """Topic labels from state appear in the output."""
    state = _make_state(
        topics=[Topic(id="t1", label="K", status="current", age_secs=60, is_current=True, title="deploy")]
    )
    text = render_static_from_state(state)
    assert "[K]" in text


def test_render_static_from_state_shows_action_text():
    """Action text from state appears in the output."""
    state = _make_state(
        actions=[Action(id="a1", topic_id="K", text="approve the plan", tier=2, age_secs=120)]
    )
    text = render_static_from_state(state)
    assert "approve the plan" in text


def test_render_static_from_state_shows_agent_role():
    """Agent role from state appears in the output."""
    state = _make_state(
        agents=[Agent(id_short="abc12345", role="coder", status="busy", topic_id="K", age_secs=300)]
    )
    text = render_static_from_state(state)
    assert "coder" in text


def test_render_static_from_state_shows_notification_text():
    """Notification text from state appears in the output."""
    state = _make_state(
        notifications=[Notification(text="agent finished task", kind="complete", age_secs=10)]
    )
    text = render_static_from_state(state)
    assert "agent finished task" in text


def test_render_static_from_state_returns_str():
    """Return type is str."""
    text = render_static_from_state(_make_state())
    assert isinstance(text, str)


def test_render_static_from_state_width_param():
    """Width parameter is accepted (no exception)."""
    text = render_static_from_state(_make_state(), width=80)
    assert "Topics" in text


def test_render_static_with_real_db(tmp_path):
    """render_static() seeds a real db and returns text with pane headers."""
    from juggle_cockpit_view import render_static
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    text = render_static(db_path=str(tmp_path / "juggle.db"))
    assert "Topics" in text
    assert "Action Items" in text
    assert "Agents" in text
    assert "Notifications" in text


def test_render_static_2d_layout():
    """Topics is left column; Action Items and Agents share top-right; Notifications is bottom-right.

    At width=120, Topics occupies leftmost 40 cols, so the first line of
    the Topics panel title must appear in the left third of every row it appears
    on.  Action Items and Agents titles must appear on the SAME line as each
    other (side-by-side) and NOT on the same line as Notifications.
    """
    state = _make_state(
        topics=[Topic(id="t1", label="K", status="current", age_secs=60, is_current=True, title="deploy")],
        actions=[Action(id="a1", topic_id="K", text="check logs", tier=2, age_secs=30)],
        agents=[Agent(id_short="ag1", role="coder", status="busy", topic_id="K", age_secs=10)],
        notifications=[Notification(text="done", kind="complete", age_secs=5)],
    )
    text = render_static_from_state(state, width=120)
    lines = text.splitlines()

    # Find the row containing "Action Items"
    actions_row = next(i for i, ln in enumerate(lines) if "Action Items" in ln)
    agents_row = next(i for i, ln in enumerate(lines) if "Agents" in ln)
    notif_row = next(i for i, ln in enumerate(lines) if "Notifications" in ln)
    topics_row = next(i for i, ln in enumerate(lines) if "Topics" in ln)

    # Topics is the leftmost panel (left third of screen)
    left_w = 120 // 3
    topics_col = lines[topics_row].find("Topics")
    assert topics_col < left_w, "Topics title must be in left column"

    # Action Items and Agents appear on the same row (side-by-side, top-right)
    assert actions_row == agents_row, "Action Items and Agents must be on the same row (side-by-side)"

    # Notifications appears below Action Items / Agents
    assert notif_row > actions_row, "Notifications must be below Action Items / Agents"

    # Notifications panel starts at left_w (right column, full width)
    notif_col = lines[notif_row].find("Notifications")
    assert notif_col >= left_w, "Notifications title must be in the right column"
