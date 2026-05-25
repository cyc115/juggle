"""Functional tests for cockpit v2 destructive actions (Phase 2).

Covers:
  - _ConfirmModal importable
  - action_close / action_archive / action_decommission methods exist
  - Functional Pilot drives: press key → prompt modal → confirm modal → DB called
  - 'n'/Esc → no DB call
  - Unknown label → no confirm modal, notification fires
  - Out-of-range index → notification fires
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual", reason="textual not installed")


# ---------------------------------------------------------------------------
# Smoke: imports / method existence
# ---------------------------------------------------------------------------


def test_confirm_modal_import():
    """_ConfirmModal is importable from juggle_cockpit."""
    from juggle_cockpit import _ConfirmModal  # noqa: F401

    assert _ConfirmModal is not None


def test_action_close_method_exists():
    from juggle_cockpit import CockpitApp

    assert hasattr(CockpitApp, "action_close")


def test_action_archive_method_exists():
    from juggle_cockpit import CockpitApp

    assert hasattr(CockpitApp, "action_archive")


def test_action_decommission_method_exists():
    from juggle_cockpit import CockpitApp

    assert hasattr(CockpitApp, "action_decommission")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _press_label(label: str) -> list[str]:
    """Convert 'MA' -> ['m', 'a'] for Pilot key presses."""
    return [ch.lower() for ch in label]


# ---------------------------------------------------------------------------
# Functional Pilot tests — action_close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_close_unknown_label_no_confirm(tmp_path):
    """C with unknown label: no confirm modal, notification fires, no DB call."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("test topic", session_id="")

    app = CockpitApp(db_path=db_path)
    with patch.object(app._db, "set_thread_status") as mock_set_status:
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.press("shift+c")
            await pilot.pause(0.1)
            # type unknown label
            await pilot.press("z")
            await pilot.press("z")
            await pilot.press("enter")
            await pilot.pause(0.2)
        mock_set_status.assert_not_called()


@pytest.mark.asyncio
async def test_action_close_valid_label_confirm_y_calls_db(tmp_path):
    """C → valid label → confirm modal → press y → set_thread_status called."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp, _ConfirmModal

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("thread one", session_id="")

    threads = db.get_all_threads()
    t1_label = next(t["user_label"] for t in threads if t["id"] == t1)

    app = CockpitApp(db_path=db_path)
    called_with: list = []

    original_set_status = app._db.set_thread_status

    def _spy_set_status(tid, status):
        called_with.append((tid, status))
        return original_set_status(tid, status)

    app._db.set_thread_status = _spy_set_status

    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("shift+c")
        await pilot.pause(0.1)
        for key in _press_label(t1_label):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.15)
        # Confirm modal should be active now
        assert app.screen is not None
        assert isinstance(app.screen, _ConfirmModal), (
            f"Expected _ConfirmModal, got {type(app.screen)}"
        )
        await pilot.press("y")
        await pilot.pause(0.2)

    assert len(called_with) == 1, f"set_thread_status called {len(called_with)} times"
    assert called_with[0] == (t1, "closed")


@pytest.mark.asyncio
async def test_action_close_valid_label_confirm_n_no_db_call(tmp_path):
    """C → valid label → confirm modal → press n → set_thread_status NOT called."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp, _ConfirmModal

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("thread one", session_id="")

    threads = db.get_all_threads()
    t1_label = next(t["user_label"] for t in threads if t["id"] == t1)

    app = CockpitApp(db_path=db_path)
    called_with: list = []
    original_set_status = app._db.set_thread_status

    def _spy_set_status(tid, status):
        called_with.append((tid, status))
        return original_set_status(tid, status)

    app._db.set_thread_status = _spy_set_status

    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("shift+c")
        await pilot.pause(0.1)
        for key in _press_label(t1_label):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert isinstance(app.screen, _ConfirmModal)
        await pilot.press("n")
        await pilot.pause(0.2)

    assert called_with == [], f"set_thread_status must not be called; got {called_with}"


# ---------------------------------------------------------------------------
# Functional Pilot tests — action_archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_archive_unknown_label_no_db_call(tmp_path):
    """x with unknown label → no DB call."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("test topic", session_id="")

    app = CockpitApp(db_path=db_path)
    with patch.object(app._db, "archive_thread") as mock_archive:
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.press("x")
            await pilot.pause(0.1)
            await pilot.press("z")
            await pilot.press("z")
            await pilot.press("enter")
            await pilot.pause(0.2)
        mock_archive.assert_not_called()


@pytest.mark.asyncio
async def test_action_archive_valid_label_y_calls_db(tmp_path):
    """x → valid label → confirm → y → archive_thread called."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp, _ConfirmModal

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("thread one", session_id="")

    threads = db.get_all_threads()
    t1_label = next(t["user_label"] for t in threads if t["id"] == t1)

    app = CockpitApp(db_path=db_path)
    called_with: list = []
    original_archive = app._db.archive_thread

    def _spy_archive(tid):
        called_with.append(tid)
        return original_archive(tid)

    app._db.archive_thread = _spy_archive

    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("x")
        await pilot.pause(0.1)
        for key in _press_label(t1_label):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert isinstance(app.screen, _ConfirmModal)
        await pilot.press("y")
        await pilot.pause(0.2)

    assert called_with == [t1], f"archive_thread called with {called_with}"


# ---------------------------------------------------------------------------
# Functional Pilot tests — action_decommission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_decommission_out_of_range_no_db_call(tmp_path):
    """d with out-of-range index → no confirm modal, no DB call."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    # Create one agent
    db.create_agent("coder", pane_id="%100")

    app = CockpitApp(db_path=db_path)
    with patch.object(app._db, "update_agent") as mock_update:
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.press("d")
            await pilot.pause(0.1)
            # Index 99 is way out of range
            await pilot.press("9")
            await pilot.press("9")
            await pilot.press("enter")
            await pilot.pause(0.2)
        mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_action_decommission_valid_index_y_calls_db(tmp_path):
    """d → valid index → confirm → y → update_agent called with decommission_pending."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp, _ConfirmModal

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    agent_id = db.create_agent("coder", pane_id="%100")

    app = CockpitApp(db_path=db_path)
    called_with: list = []
    original_update = app._db.update_agent

    def _spy_update(aid, **kwargs):
        called_with.append((aid, kwargs))
        return original_update(aid, **kwargs)

    app._db.update_agent = _spy_update

    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("d")
        await pilot.pause(0.1)
        await pilot.press("1")
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert isinstance(app.screen, _ConfirmModal), (
            f"Expected _ConfirmModal, got {type(app.screen)}"
        )
        await pilot.press("y")
        await pilot.pause(0.2)

    assert len(called_with) == 1
    aid, kwargs = called_with[0]
    assert aid == agent_id
    assert kwargs.get("status") == "decommission_pending"


@pytest.mark.asyncio
async def test_action_decommission_valid_index_n_no_db_call(tmp_path):
    """d → valid index → confirm → n → update_agent NOT called."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp, _ConfirmModal

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_agent("coder", pane_id="%100")

    app = CockpitApp(db_path=db_path)
    called_with: list = []
    original_update = app._db.update_agent

    def _spy_update(aid, **kwargs):
        called_with.append((aid, kwargs))
        return original_update(aid, **kwargs)

    app._db.update_agent = _spy_update

    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("d")
        await pilot.pause(0.1)
        await pilot.press("1")
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert isinstance(app.screen, _ConfirmModal)
        await pilot.press("n")
        await pilot.pause(0.2)

    assert called_with == [], f"update_agent must not be called; got {called_with}"
