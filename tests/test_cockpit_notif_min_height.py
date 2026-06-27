"""Regression pins — the notification pane must always show ≥1 full row.

Incident (2026-06-26): in the cockpit TUI the #notif-region pane could be
squeezed below one visible notification row on a short terminal — the
Action Items + Agents row (#upper) pushed it off-screen — and a manual
HSplitter drag could shrink it below that floor. Required behaviour:
#notif-region ALWAYS shows at least one full notification row (the panel
border-top+title, one content row, border-bottom = ``_MIN_NOTIF_HEIGHT``),
regardless of terminal size or drag.

These tests assert the pane is actually ON-SCREEN, not merely that its
``size.height`` is large: a widget can report height 3 while positioned past
the screen bottom (the original bug — #upper overflowed and shoved it off).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cockpit_css import _MIN_NOTIF_HEIGHT  # noqa: E402


def _make_app(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    # Seed one notification for the current session so the panel renders a row.
    db._set_session_key_external("session_id", "S1")
    db.add_notification_v2(None, "hello from a notification", "S1")
    return CockpitApp(db_path=db_path)


def _visible_rows(app, widget):
    """Rows of ``widget`` that actually fall within the screen viewport."""
    top = max(widget.region.y, 0)
    bottom = min(widget.region.bottom, app.size.height)
    return max(0, bottom - top)


def _drag_hsplitter(app, screen_y):
    """Simulate dragging the HSplitter handle to absolute ``screen_y``."""
    from textual.events import MouseMove
    from juggle_cockpit_widgets import HSplitter

    hs = next(iter(app.query(HSplitter)))
    upper = app.query_one("#upper")
    notif = app.query_one("#notif-region")
    hs._dragging = True
    hs._drag_start_y = upper.size.height  # handle sits just below #upper
    hs._top_start_h = upper.size.height
    hs._bottom_start_h = notif.size.height
    event = MouseMove(
        hs, x=0, y=screen_y,
        delta_x=0, delta_y=0, button=0,
        shift=False, meta=False, ctrl=False,
        screen_x=0, screen_y=screen_y, style=None,
    )
    hs.on_mouse_move(event)


# ---------------------------------------------------------------------------
# 1 — short terminal must keep the whole notification panel on-screen
# ---------------------------------------------------------------------------


async def test_short_terminal_floors_notif_region_visible(tmp_path, monkeypatch):
    """At a short terminal #notif-region must be fully visible (≥ _MIN_NOTIF_HEIGHT rows).

    Pre-fix RED: #upper's percentage height overflowed the screen, leaving only
    2 of the panel's 3 rows visible (bottom border + content row clipped).
    """
    monkeypatch.setattr("juggle_cockpit._COL_RATIOS", [0.30, 0.40, 0.30])
    monkeypatch.setattr("juggle_cockpit._NOTIF_RATIO", 30)
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 12)) as pilot:
        await pilot.pause(0.2)
        notif = app.query_one("#notif-region")
        visible = _visible_rows(app, notif)
        assert visible >= _MIN_NOTIF_HEIGHT, (
            f"#notif-region only {visible} rows on-screen at 100x12 "
            f"(need {_MIN_NOTIF_HEIGHT}); #upper squeezed it off-screen"
        )


# ---------------------------------------------------------------------------
# 2 — dragging the handle to the extreme must not shrink notif below the floor
# ---------------------------------------------------------------------------


async def test_drag_extreme_down_keeps_notif_floor(tmp_path, monkeypatch):
    """Dragging the HSplitter fully down (maximising #upper) keeps notif ≥ floor.

    The drag floor must derive from _MIN_NOTIF_HEIGHT, not a magic constant.
    """
    monkeypatch.setattr("juggle_cockpit._COL_RATIOS", [0.30, 0.40, 0.30])
    monkeypatch.setattr("juggle_cockpit._NOTIF_RATIO", 30)
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        _drag_hsplitter(app, screen_y=9999)  # all the way down
        await pilot.pause(0.2)
        notif = app.query_one("#notif-region")
        assert notif.size.height >= _MIN_NOTIF_HEIGHT, (
            f"#notif-region collapsed to {notif.size.height} after extreme drag "
            f"(floor is {_MIN_NOTIF_HEIGHT})"
        )
        assert _visible_rows(app, notif) >= _MIN_NOTIF_HEIGHT


# ---------------------------------------------------------------------------
# 3 — drag #upper tall, then SHRINK the terminal: notif must stay visible
# ---------------------------------------------------------------------------


async def test_resize_after_drag_keeps_notif_visible(tmp_path, monkeypatch):
    """Drag #upper tall, then shrink the terminal — notif must remain on-screen.

    Pre-fix RED: the drag left #upper with an explicit large cell height; on
    shrink #upper kept that height, overflowed the screen, and pushed
    #notif-region entirely off-screen (0 rows visible).
    """
    monkeypatch.setattr("juggle_cockpit._COL_RATIOS", [0.30, 0.40, 0.30])
    monkeypatch.setattr("juggle_cockpit._NOTIF_RATIO", 30)
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        _drag_hsplitter(app, screen_y=9999)  # make #upper as tall as possible
        await pilot.pause(0.1)
        await pilot.resize_terminal(100, 12)
        await pilot.pause(0.2)
        notif = app.query_one("#notif-region")
        visible = _visible_rows(app, notif)
        assert visible >= _MIN_NOTIF_HEIGHT, (
            f"#notif-region only {visible} rows on-screen after drag+shrink "
            f"(need {_MIN_NOTIF_HEIGHT}); #upper's dragged cell height starved it"
        )


# ---------------------------------------------------------------------------
# 4 — graph mode hosts the same region; it too must keep the floor
# ---------------------------------------------------------------------------


async def test_graph_mode_short_terminal_floors_region(tmp_path, monkeypatch):
    """In graph mode (#graph-scroll hosted in #notif-region) the floor still holds."""
    monkeypatch.setattr("juggle_cockpit._COL_RATIOS", [0.30, 0.40, 0.30])
    monkeypatch.setattr("juggle_cockpit._NOTIF_RATIO", 30)
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 12)) as pilot:
        await pilot.pause(0.2)
        app.action_toggle_graph()
        await pilot.pause(0.2)
        notif = app.query_one("#notif-region")
        visible = _visible_rows(app, notif)
        assert visible >= _MIN_NOTIF_HEIGHT, (
            f"#notif-region only {visible} rows on-screen in graph mode "
            f"(need {_MIN_NOTIF_HEIGHT})"
        )
