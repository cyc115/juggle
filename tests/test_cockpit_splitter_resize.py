"""TDD tests for cockpit resize breakpoint and splitter drag bugs.

Cycles:
  1. on_resize uses terminal width, not topics pane width (Bug 1)
  2. Splitter drag produces percent-based widths with min-% floor (Bug 2)
  3. Full drag-then-resize: no pane collapse (Bug 1 + Bug 2 interaction)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_app(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    return CockpitApp(db_path=db_path)


# ---------------------------------------------------------------------------
# Cycle 1 — on_resize must use terminal width (Bug 1)
# ---------------------------------------------------------------------------


async def test_resize_uses_terminal_width_not_topics_pane_width(tmp_path, monkeypatch):
    """topics must stay visible after resize when terminal is wide but topics pane is narrow.

    Bug: on_resize used pick_breakpoint(topics.size.width) → 20-cell pane → "narrow"
    → topics hidden even though terminal is 200 (≥120 = "wide").
    Fix: use event.size.width for the breakpoint decision.
    """
    monkeypatch.setattr("juggle_cockpit._COL_RATIOS", [0.30, 0.40, 0.30])
    app = _make_app(tmp_path)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause(0.1)

        topics = app.query_one("#topics")
        # Simulate a user drag that left topics very narrow (20 cells on a 200-wide terminal)
        topics.styles.width = 20
        await pilot.pause(0.05)

        # Trigger resize at the same wide terminal size
        await pilot.resize_terminal(200, 40)
        await pilot.pause(0.1)

        assert topics.size.width > 0, (
            f"topics collapsed after resize; on_resize used topics pane width (20) "
            f"instead of terminal width (200), got topics.size.width={topics.size.width}"
        )


# ---------------------------------------------------------------------------
# Cycle 2 — Splitter drag must produce percent-based widths with min-% floor (Bug 2)
# ---------------------------------------------------------------------------


async def test_splitter_drag_uses_percent_with_min_floor(tmp_path, monkeypatch):
    """After an extreme left drag, topics width must be ≥ _MIN_TOPICS_PCT% of terminal.

    Bug: on_mouse_move set topics.styles.width = 8 (cells, absolute floor).
    Fix: convert to percent with clamp [_MIN_TOPICS_PCT, _MAX_TOPICS_PCT].
    """
    from textual.events import MouseMove
    from juggle_cockpit_widgets import Splitter
    from juggle_cockpit import _MIN_TOPICS_PCT

    monkeypatch.setattr("juggle_cockpit._COL_RATIOS", [0.30, 0.40, 0.30])
    app = _make_app(tmp_path)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause(0.1)

        topics = app.query_one("#topics")
        right = app.query_one("#right")
        h_splitter = next(
            s for s in app.query(Splitter) if s._left_id == "topics"
        )

        left_w = topics.size.width    # ~60 (30% of 200)
        right_w = right.size.width   # ~139

        # Simulate drag start state
        h_splitter._dragging = True
        h_splitter._drag_start_x = left_w
        h_splitter._left_start_w = left_w
        h_splitter._right_start_w = right_w

        # Drag far left: screen_x=2 → delta≈-58 → would give 2 cells without floor
        event = MouseMove(
            h_splitter, x=2, y=0,
            delta_x=0, delta_y=0, button=0,
            shift=False, meta=False, ctrl=False,
            screen_x=2, screen_y=0, style=None,
        )
        h_splitter.on_mouse_move(event)
        await pilot.pause(0.1)

        # Must be ≥ _MIN_TOPICS_PCT% of terminal width (not the hard cell floor of 8)
        min_topics_cells = int(200 * _MIN_TOPICS_PCT / 100)
        assert topics.size.width >= min_topics_cells, (
            f"topics.size.width={topics.size.width} < {min_topics_cells} "
            f"({_MIN_TOPICS_PCT}% of 200); drag used cell floor instead of percent floor"
        )
        assert right.size.width > 0, f"right pane collapsed: {right.size.width}"


# ---------------------------------------------------------------------------
# Cycle 3 — drag to extreme then resize: no pane collapse (Bug 1 + Bug 2 together)
# ---------------------------------------------------------------------------


async def test_drag_then_resize_no_pane_collapse(tmp_path, monkeypatch):
    """After dragging topics narrow then triggering a resize, topics must remain visible.

    Bug interaction: Bug 2 sets topics to 8 cells → Bug 1 reads 8 for bp
    → "narrow" breakpoint → topics hidden even though terminal is 200 wide.
    """
    from textual.events import MouseMove
    from juggle_cockpit_widgets import Splitter

    monkeypatch.setattr("juggle_cockpit._COL_RATIOS", [0.30, 0.40, 0.30])
    app = _make_app(tmp_path)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause(0.1)

        topics = app.query_one("#topics")
        right = app.query_one("#right")
        h_splitter = next(
            s for s in app.query(Splitter) if s._left_id == "topics"
        )

        left_w = topics.size.width
        right_w = right.size.width

        # Simulate extreme-left drag
        h_splitter._dragging = True
        h_splitter._drag_start_x = left_w
        h_splitter._left_start_w = left_w
        h_splitter._right_start_w = right_w
        event = MouseMove(
            h_splitter, x=2, y=0,
            delta_x=0, delta_y=0, button=0,
            shift=False, meta=False, ctrl=False,
            screen_x=2, screen_y=0, style=None,
        )
        h_splitter.on_mouse_move(event)
        await pilot.pause(0.05)

        # Trigger resize at same wide terminal size
        await pilot.resize_terminal(200, 40)
        await pilot.pause(0.1)

        assert topics.size.width > 0, (
            f"topics collapsed after drag+resize: width={topics.size.width}; "
            "Bug 1 read the small post-drag cell width and picked 'narrow' breakpoint"
        )
        assert right.size.width > 0, (
            f"right collapsed after drag+resize: width={right.size.width}"
        )
