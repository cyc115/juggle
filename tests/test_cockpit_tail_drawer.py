"""Cockpit tail drawer / focus-pane tests: action_focus_pane, action_tail_toggle, _TailModal. Split from test_cockpit_features_v2.py (2026-06-10)."""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual", reason="textual not installed")

# ---------------------------------------------------------------------------
# Phase 5 — Task 12+13: tail drawer state, action_focus_pane, action_tail_toggle
# ---------------------------------------------------------------------------


def test_action_focus_pane_method_exists():
    from juggle_cockpit import CockpitApp
    assert hasattr(CockpitApp, "action_focus_pane")


def test_action_tail_toggle_method_exists():
    from juggle_cockpit import CockpitApp
    assert hasattr(CockpitApp, "action_tail_toggle")


def test_tail_drawer_state_attrs_removed_from_init():
    """Drawer attrs _tail_active / _tail_pane_id must NOT exist — replaced by _TailModal."""
    import inspect
    from juggle_cockpit import CockpitApp
    src = inspect.getsource(CockpitApp.__init__)
    assert "_tail_active" not in src, "_tail_active drawer state should be removed"
    assert "_tail_pane_id" not in src, "_tail_pane_id drawer state should be removed"


@pytest.mark.asyncio
async def test_action_focus_pane_calls_tmux_with_correct_pane_id(tmp_path):
    """f → type 1 → enter → _tmux_focus_pane called with agent's pane_id."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp
    import juggle_cockpit

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_agent("coder", pane_id="%100")

    app = CockpitApp(db_path=db_path)
    calls: list = []

    def mock_focus(pane_id: str) -> bool:
        calls.append(pane_id)
        return True

    async with app.run_test(size=(160, 40)) as pilot:
        with patch.object(juggle_cockpit, "_tmux_focus_pane", mock_focus):
            await pilot.press("f")
            await pilot.pause(0.1)
            await pilot.press("1")
            await pilot.press("enter")
            await pilot.pause(0.3)

    assert calls == ["%100"], f"Expected ['%100'], got {calls}"


@pytest.mark.asyncio
async def test_action_tail_toggle_pushes_modal_and_injects_capture(tmp_path):
    """t → 1 → enter pushes _TailModal; injected capture_fn calls _tmux_capture_pane."""
    import juggle_cockpit
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_modals import _TailModal
    from juggle_db import JuggleDB

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_agent("coder", pane_id="%200")

    app = CockpitApp(db_path=db_path)
    capture_calls: list = []

    def mock_capture(pane_id: str, lines: int = 20) -> str:
        capture_calls.append(pane_id)
        return "line1\nline2"

    async with app.run_test(size=(160, 40)) as pilot:
        with patch.object(juggle_cockpit, "_tmux_capture_pane", mock_capture):
            await pilot.press("t")
            await pilot.pause(0.1)
            await pilot.press("1")
            await pilot.press("enter")
            await pilot.pause(0.3)

            # _TailModal should now be on top of screen_stack
            assert len(pilot.app.screen_stack) == 2, (
                f"Expected _TailModal on stack, got depth {len(pilot.app.screen_stack)}"
            )
            assert isinstance(pilot.app.screen_stack[-1], _TailModal), (
                f"Expected _TailModal, got {type(pilot.app.screen_stack[-1])}"
            )
            # The injected capture_fn (which wraps mock_capture) should have been called
            assert len(capture_calls) >= 1, "_tmux_capture_pane not called via injected fn"
            assert capture_calls[0] == "%200", f"capture called with wrong pane: {capture_calls[0]}"
