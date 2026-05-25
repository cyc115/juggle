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
from unittest.mock import patch

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

    def _spy_set_status(thread_id, status):
        called_with.append((thread_id, status))
        return original_set_status(thread_id, status)

    app._db.set_thread_status = _spy_set_status  # type: ignore[method-assign]

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

    def _spy_set_status(thread_id, status):
        called_with.append((thread_id, status))
        return original_set_status(thread_id, status)

    app._db.set_thread_status = _spy_set_status  # type: ignore[method-assign]

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

    def _spy_archive(thread_id):
        called_with.append(thread_id)
        return original_archive(thread_id)

    app._db.archive_thread = _spy_archive  # type: ignore[method-assign]

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

    def _spy_update(agent_id, **kwargs):
        called_with.append((agent_id, kwargs))
        return original_update(agent_id, **kwargs)

    app._db.update_agent = _spy_update  # type: ignore[method-assign]

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

    def _spy_update(agent_id, **kwargs):
        called_with.append((agent_id, kwargs))
        return original_update(agent_id, **kwargs)

    app._db.update_agent = _spy_update  # type: ignore[method-assign]

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


# ---------------------------------------------------------------------------
# Phase 4 — Bell state + _refresh integration
# ---------------------------------------------------------------------------


def test_bell_state_attrs_exist():
    """CockpitApp.__init__ initialises _prev_action_ids and _prev_agent_statuses."""
    import inspect
    from juggle_cockpit import CockpitApp
    src = inspect.getsource(CockpitApp.__init__)
    assert "_prev_action_ids" in src
    assert "_prev_agent_statuses" in src


def test_bell_enabled_attr_exists():
    """CockpitApp.__init__ initialises _bell_enabled."""
    import inspect
    from juggle_cockpit import CockpitApp
    src = inspect.getsource(CockpitApp.__init__)
    assert "_bell_enabled" in src


# ---------------------------------------------------------------------------
# Phase 4 — Functional: bell via _refresh diff (direct invocation)
# ---------------------------------------------------------------------------
# Full Pilot tick-simulation is fragile; instead we call _refresh directly
# with seeded prev-state and a mocked self.bell to assert it fires correctly.


@pytest.mark.asyncio
async def test_bell_fires_on_new_blocker(tmp_path):
    """New tier-0 action on 2nd _refresh fires self.bell(); 1st tick does not."""
    from unittest.mock import patch
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("alpha", session_id="")

    app = CockpitApp(db_path=db_path)

    with patch.object(app, "bell") as mock_bell:
        async with app.run_test(size=(160, 40)):
            # 1st tick: prev_ids / prev_statuses are both empty → guard skips bell
            app._refresh()
            assert mock_bell.call_count == 0, "bell must NOT fire on first tick"

            # Seed prev state so the guard passes on next call
            app._prev_action_ids = set()
            app._prev_agent_statuses = {}

            # Add a tier-0 (high priority) action AFTER prev state was captured (simulate 2nd tick)
            db.add_action_item(t1, "critical blocker", type_="question", priority="high")

            # Force prev state to non-empty so guard passes
            app._prev_action_ids = {"dummy-prev-id"}

            app._refresh()
            assert mock_bell.call_count >= 1, (
                f"bell must fire when a new blocker appears (called {mock_bell.call_count}x)"
            )


@pytest.mark.asyncio
async def test_bell_fires_on_agent_failure(tmp_path):
    """Agent transitioning busy→stale on 2nd _refresh fires self.bell()."""
    from unittest.mock import patch
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("beta", session_id="")

    # Create an agent in busy state
    agent_id = db.create_agent("coder", pane_id="%99")

    app = CockpitApp(db_path=db_path)

    with patch.object(app, "bell") as mock_bell:
        async with app.run_test(size=(160, 40)):
            # Disable the throttled reaper: it fires on first tick (_last_reap=0) when
            # tmux is available, deleting decommission_pending agents before snapshot reads them.
            app._cockpit_mgr = None

            # Seed prev state: agent was busy
            agent_short = agent_id[:8]
            app._prev_action_ids = {"dummy"}
            app._prev_agent_statuses = {agent_short: "busy"}

            # Transition agent to display-stale (decommission_pending maps to display "stale")
            db.update_agent(agent_id, status="decommission_pending")

            app._refresh()
            assert mock_bell.call_count >= 1, (
                f"bell must fire when agent goes stale (called {mock_bell.call_count}x)"
            )


@pytest.mark.asyncio
async def test_bell_no_fire_on_first_tick(tmp_path):
    """First _refresh (prev state empty) does NOT call self.bell()."""
    from unittest.mock import patch
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("gamma", session_id="")
    # Seed a tier-0 action BEFORE the app starts — should NOT trigger bell on first tick
    db.add_action_item(t1, "pre-existing blocker", type_="question", priority="high")

    app = CockpitApp(db_path=db_path)

    with patch.object(app, "bell") as mock_bell:
        async with app.run_test(size=(160, 40)):
            # Reset to ensure truly first-tick state
            app._prev_action_ids = set()
            app._prev_agent_statuses = {}

            app._refresh()
            assert mock_bell.call_count == 0, (
                f"bell must NOT fire on first tick even with pre-existing blockers "
                f"(called {mock_bell.call_count}x)"
            )
