"""Functional Pilot tests for cockpit action_filter state cycling. Split from test_cockpit_features_v2.py (2026-06-10)."""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual", reason="textual not installed")

# ---------------------------------------------------------------------------
# Phase 3 — action_filter functional Pilot tests
# ---------------------------------------------------------------------------


def test_action_filter_method_exists():
    from juggle_cockpit import CockpitApp
    assert hasattr(CockpitApp, "action_filter")


def test_filter_state_in_init():
    """CockpitApp.__init__ initialises _filter dict with pane keys."""
    from juggle_cockpit import CockpitApp
    import inspect
    src = inspect.getsource(CockpitApp.__init__)
    assert "_filter" in src


@pytest.mark.asyncio
async def test_action_filter_sets_state_and_resets_offset(tmp_path):
    """`/` → type substring → enter sets _filter[pane] and resets offset to 0.

    Note: Tab binding is consumed by Textual's focus system in Pilot; we set
    _active_pane directly. The filter behavior itself (not Tab navigation) is tested.
    """
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("deploy db", session_id="")
    db.add_action_item(t1, "deploy DB migration", type_="question")
    db.add_action_item(t1, "write docs", type_="question")

    app = CockpitApp(db_path=db_path)

    async with app.run_test(size=(160, 40)) as pilot:
        # Set active pane directly (Tab binding is swallowed by Textual focus system)
        app._active_pane = "actions"
        # Give a non-zero offset to verify it resets
        app._offsets["actions"] = 2

        # Press / (slash) to open filter modal
        await pilot.press("/")
        await pilot.pause(0.1)

        # Type "deploy" and submit
        for ch in "deploy":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)

    # Filter state set
    assert app._filter["actions"] == "deploy", f"Expected 'deploy', got {app._filter['actions']!r}"
    # Offset reset to 0
    assert app._offsets["actions"] == 0, f"Expected offset 0, got {app._offsets['actions']}"


@pytest.mark.asyncio
async def test_action_filter_blank_submit_clears_filter(tmp_path):
    """`/` → blank submit clears existing filter and resets offset."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("alpha", session_id="")

    app = CockpitApp(db_path=db_path)

    async with app.run_test(size=(160, 40)) as pilot:
        # Set active pane and existing filter/offset directly
        app._active_pane = "actions"
        app._filter["actions"] = "something"
        app._offsets["actions"] = 3

        await pilot.press("/")
        await pilot.pause(0.1)
        # Submit without typing anything (blank) → should clear filter
        await pilot.press("enter")
        await pilot.pause(0.2)

    # Filter cleared
    assert app._filter["actions"] == "", f"Expected '', got {app._filter['actions']!r}"
    # Offset reset
    assert app._offsets["actions"] == 0, f"Expected offset 0, got {app._offsets['actions']}"


@pytest.mark.asyncio
async def test_action_filter_esc_with_active_filter_clears_and_resets_offset(tmp_path):
    """Pressing Esc outside modal when filter is active clears filter + resets offset."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("beta", session_id="")

    app = CockpitApp(db_path=db_path)

    async with app.run_test(size=(160, 40)) as pilot:
        # Set active pane + filter state directly, then press Esc at app level
        app._active_pane = "actions"
        app._filter["actions"] = "deploy"
        app._offsets["actions"] = 4
        app._filter["agents"] = "coder"

        await pilot.pause(0.05)
        # Press Esc — no modal open, filter is active → should clear all filters
        await pilot.press("escape")
        await pilot.pause(0.2)

    # All filters cleared
    assert app._filter["actions"] == "", f"actions filter: {app._filter['actions']!r}"
    assert app._filter["agents"] == "", f"agents filter: {app._filter['agents']!r}"
    # Active pane (actions) offset reset
    assert app._offsets["actions"] == 0, f"Expected offset 0, got {app._offsets['actions']}"


@pytest.mark.asyncio
async def test_action_filter_esc_in_modal_leaves_filter_unchanged(tmp_path):
    """Pressing Esc inside the filter prompt modal leaves existing filter unchanged.

    Note: CockpitApp.on_key checks screen_stack depth to avoid intercepting
    Esc when a modal is open. This test verifies that guard works.
    """
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("gamma", session_id="")

    app = CockpitApp(db_path=db_path)

    async with app.run_test(size=(160, 40)) as pilot:
        app._active_pane = "actions"
        # Pre-set a filter
        app._filter["actions"] = "existing"

        # Open filter modal, then Esc out of the modal
        await pilot.press("/")
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.2)

    # Filter unchanged — Esc in modal means "cancel", not "clear"
    assert app._filter["actions"] == "existing", (
        f"Expected 'existing', got {app._filter['actions']!r}"
    )

