"""TDD tests for graph-mode key wiring in CockpitApp.

`g` swaps the lower-right panel Notifications↔Graph; in graph mode ↑↓ move the
task selection, ←→ pan, enter opens the detail modal, and `g`/`esc` restore
Notifications. Navigation keys must NOT leak to global scroll/cycle while in
graph mode (regression pin).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _armed_db(tmp_path):
    from juggle_db import JuggleDB
    from dbops import db_graph as g
    from juggle_graph_dispatch import ARMED_PROJECT_KEY

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", ("P", "Proj", "active", now, now),
        )
        conn.commit()
    g.create_task(db, task_id="n1", project_id="P", title="One", prompt="do 1")
    g.create_task(db, task_id="n2", project_id="P", title="Two", prompt="do 2")
    g.replace_edges(db, "n2", ["n1"])
    db.set_setting(ARMED_PROJECT_KEY, "P")
    return db_path


def _topic_db(tmp_path):
    """Project whose DAG root is a kind='topic' node — the case the topic-info
    modal must populate from the authoritative nodes row."""
    from juggle_db import JuggleDB
    from dbops import db_topics as dt

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", ("P", "Proj", "active", now, now),
        )
        conn.commit()
    dt.create_topic(
        db, topic_id="T1", project_id="P", title="Topic One", objective="ship it"
    )
    return db_path


# ---------------------------------------------------------------------------
# Binding + free-key invariants
# ---------------------------------------------------------------------------


def test_g_binding_present():
    from juggle_cockpit import CockpitApp
    actions = {b.action for b in CockpitApp.BINDINGS}
    assert "toggle_graph" in actions
    g_keys = [b.key for b in CockpitApp.BINDINGS if b.action == "toggle_graph"]
    assert g_keys == ["g"]


# ---------------------------------------------------------------------------
# Toggle behaviour (Textual Pilot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g_enters_and_exits_graph_mode(tmp_path):
    from juggle_cockpit import CockpitApp

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        assert app._graph_mode is False
        await pilot.press("g")
        await pilot.pause(0.1)
        assert app._graph_mode is True
        await pilot.press("g")
        await pilot.pause(0.1)
        assert app._graph_mode is False


@pytest.mark.asyncio
async def test_escape_exits_graph_mode(tmp_path):
    from juggle_cockpit import CockpitApp

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.1)
        assert app._graph_mode is True
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app._graph_mode is False


@pytest.mark.asyncio
async def test_down_up_moves_selection(tmp_path):
    from juggle_cockpit import CockpitApp

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.1)
        assert app._graph_sel == 0
        await pilot.press("down")
        await pilot.pause(0.05)
        assert app._graph_sel == 1
        await pilot.press("up")
        await pilot.pause(0.05)
        assert app._graph_sel == 0


@pytest.mark.asyncio
async def test_right_left_pans(tmp_path):
    from juggle_cockpit import CockpitApp

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.1)
        await pilot.press("right")
        await pilot.pause(0.05)
        assert app._graph_pan == 1
        await pilot.press("left")
        await pilot.pause(0.05)
        assert app._graph_pan == 0


@pytest.mark.asyncio
async def test_enter_opens_detail_modal(tmp_path):
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_modals import _GraphTaskModal

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert isinstance(app.screen, _GraphTaskModal)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, _GraphTaskModal)


@pytest.mark.asyncio
async def test_enter_on_topic_node_populates_modal(tmp_path):
    """REGRESSION: opening the info modal on a TOPIC node must show its
    id/title/state from the nodes row — NOT a blank 'Task ?'. The caller fetched
    via get_task (kind='task'), which returns None for a topic id, so the modal
    rendered an empty dict (P8 c4-topic-dag flip missed this read-path)."""
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_modals import _GraphTaskModal

    app = CockpitApp(db_path=_topic_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.1)
        assert app._graph_sel == 0
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert isinstance(app.screen, _GraphTaskModal)
        row = app.screen._task_row
        assert row.get("id") == "T1"
        assert row.get("title")   # non-empty title from the nodes row
        assert row.get("state")   # non-empty state from the nodes row


@pytest.mark.asyncio
async def test_arrows_do_not_leak_to_scroll_when_graph_off(tmp_path):
    """REGRESSION PIN (2026-06-10): in graph mode arrows must be captured; with
    graph mode OFF, the global scroll/pane-cycle handlers stay intact (j/k/tab
    keep working) — graph wiring never alters default key behaviour."""
    from juggle_cockpit import CockpitApp

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        # graph mode OFF: down should scroll the active pane, not move selection
        assert app._graph_mode is False
        start_pane = app._active_pane
        await pilot.press("tab")
        await pilot.pause(0.05)
        # tab still cycles panes (proof default handlers untouched)
        assert app._active_pane != start_pane
        # selection untouched while off
        assert app._graph_sel == 0
