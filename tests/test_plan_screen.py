"""PlanScreen tests (cockpit-plan-view, 2026-07-02). The full-screen Plan view
renders juggle_plan_layout as dependency-wave COLUMNS: an anchor stub, wave
headers with parallel-count vs free-slot budget, an amber critical-path lane,
selection auto-expanding member tasks in place, z-fold, arrow-key pan, and
Enter opening the selected topic. Pilot tests on a synthetic 20-node fixture
(15-wide fan-in wave) — the exact shape the removed gitlog railroad IndexError'd
on — proving the columnar renderer is bounds-safe."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _project(conn, pid, name="Proj"):
    now = _now()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at,last_active) VALUES(?,?,?,?,?)",
        (pid, name, "active", now, now),
    )


def _topic(conn, tid, pid, title, state="open", parent_id=None):
    now = _now()
    conn.execute(
        "INSERT INTO nodes(id,kind,title,objective,state,project_id,parent_id,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (tid, "topic" if parent_id is None else "task", title, "", state, pid,
         parent_id, now, now),
    )


def _edge(conn, node_id, depends_on_id):
    conn.execute(
        "INSERT OR IGNORE INTO node_edges(node_id, depends_on_id, kind) "
        "VALUES(?,?,'dep')", (node_id, depends_on_id),
    )


def _fixture_20_node(db_path):
    """One project: a verified anchor, a ready root (with 2 member tasks), a
    running node, a 15-wide fan-out (wave 1) off the ready root, and a 4-node
    convergence tail (wave 2) — 20 open topics total. The 15-wide wave is the
    wide fan-in shape the old railroad IndexError'd on."""
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=db_path)
    db.init_db()
    with db._connect() as conn:
        _project(conn, "P", "Proj")
        _topic(conn, "anchor", "P", "Anchor", state="verified")
        _topic(conn, "fan-root", "P", "Fan Root", state="ready")
        _topic(conn, "runner", "P", "Runner", state="running")
        conn.execute(
            "INSERT INTO agent_runs(thread_id, topic_id, role, model, "
            "input_prompt, dispatched_at, status) VALUES(?,?,?,?,?,?,?)",
            ("th-fixture", "runner", "coder", "sonnet", "do it", _now(), "dispatched"),
        )
        for i in range(15):
            _topic(conn, f"w{i}", "P", f"Wide {i}", state="open")
            _edge(conn, f"w{i}", "fan-root")
        for i in range(4):
            _topic(conn, f"t{i}", "P", f"Tail {i}", state="open")
            _edge(conn, f"t{i}", "w0")
            _edge(conn, f"t{i}", "w1")
        _topic(conn, "task-a", "P", "Task A", state="verified", parent_id="fan-root")
        _topic(conn, "task-b", "P", "Task B", state="open", parent_id="fan-root")
        _edge(conn, "task-b", "task-a")
        conn.commit()
    return db


def _second_project(db):
    with db._connect() as conn:
        _project(conn, "P2", "Second")
        _topic(conn, "only", "P2", "Only Topic", state="ready")
        conn.commit()


def _dags(db):
    from juggle_cockpit_graph_dag import load_graph_dags

    with db._connect() as conn:
        return load_graph_dags(conn)


def _mk_app(dags, db):
    from textual.app import App
    from juggle_cockpit_plan_screen import PlanScreen

    class _Harness(App):
        def on_mount(self):
            self.push_screen(PlanScreen(dags, "P", db))

    return _Harness()


@pytest.mark.asyncio
async def test_wave_headers_show_parallel_and_slot_budget(tmp_path):
    from juggle_cockpit_plan_screen import PlanScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PlanScreen)
        text = screen._body_text
        assert "WAVE 1" in text and "WAVE 2" in text
        # Wave 1 is the runnable-now wave: parallel count vs free agent slots.
        assert "parallel" in text
        assert "slots free" in text


@pytest.mark.asyncio
async def test_anchor_stub_reports_verified_count(tmp_path):
    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        assert app.screen._layout.anchor_count == 1
        assert "1 done" in app.screen._body_text or "1 verified" in app.screen._body_text


@pytest.mark.asyncio
async def test_critical_path_lane_and_footer_present(tmp_path):
    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen._layout.critical_path  # root -> a wide node -> a tail
        assert "critical path" in screen._body_text
        # Amber critical lane uses a double-line box glyph absent from quiet cards.
        assert any(g in screen._body_text for g in ("║", "═"))


@pytest.mark.asyncio
async def test_selection_expands_member_tasks_in_place(tmp_path):
    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        screen = app.screen
        # Selection starts on the first wave-0 card: fan-root (has member tasks).
        assert screen._selectable()[screen._sel].id == "fan-root"
        assert "Task B" in screen._body_text  # member task expanded in place
        # Moving off fan-root collapses its member tasks.
        await pilot.press("j")
        await pilot.pause()
        assert screen._selectable()[screen._sel].id != "fan-root"
        assert "Task B" not in screen._body_text


@pytest.mark.asyncio
async def test_z_folds_overflow_behind_summary(tmp_path):
    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert "WAVE 2" in screen._body_text  # wave-2 column shown when expanded
        await pilot.press("z")
        await pilot.pause()
        assert "more open node" in screen._body_text  # fold summary
        assert "WAVE 2" not in screen._body_text  # trailing waves collapsed away
        await pilot.press("z")
        await pilot.pause()
        assert "WAVE 2" in screen._body_text  # expanded again


@pytest.mark.asyncio
async def test_wide_fan_in_is_bounds_safe(tmp_path):
    """The 15-wide wave is exactly the shape the removed gitlog railroad
    IndexError'd on. Navigating through every card must never crash."""
    from juggle_cockpit_plan_screen import PlanScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for _ in range(30):
            await pilot.press("j")
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)  # still alive, no IndexError


@pytest.mark.asyncio
async def test_arrow_pan_on_overflow_does_not_crash(tmp_path):
    from juggle_cockpit_plan_screen import PlanScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(60, 30)) as pilot:  # narrow: columns overflow
        await pilot.pause()
        screen = app.screen
        start_pan = screen._pan
        await pilot.press("right")
        await pilot.press("right")
        await pilot.pause()
        assert screen._pan > start_pan
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("left")
        await pilot.press("left")
        await pilot.press("left")
        await pilot.pause()
        assert screen._pan == 0  # clamped, never negative


@pytest.mark.asyncio
async def test_enter_opens_topic_detail(tmp_path):
    from juggle_cockpit_plan_screen import PlanScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.1)
        # A detail modal is pushed over the PlanScreen.
        assert not isinstance(app.screen, PlanScreen)


@pytest.mark.asyncio
async def test_tab_cycles_projects(tmp_path):
    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    _second_project(db)
    app = _mk_app(_dags(db), db)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen._dag.project_id == "P"
        await pilot.press("tab")
        await pilot.pause()
        assert screen._dag.project_id == "P2"
        assert "Only Topic" in screen._body_text
