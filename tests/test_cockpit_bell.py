"""Cockpit bell-on-new-blocker tests: bell state attrs + _refresh diff firing rules. Split from test_cockpit_features_v2.py (2026-06-10)."""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual", reason="textual not installed")


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


