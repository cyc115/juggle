"""FrontierScreen tests (fr-screen, 2026-07-02) — replaces the removed
GitlogScreen/RailroadScreen. Pilot tests on a synthetic 20-node fixture:
wave bands, critical rail, drill-in/out, wave fold, Tab project-cycle, detail
pane updates, and that both `l` (Graph mode) and `G` open this ONE screen."""
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
    """One project: a verified anchor, a ready root, a 15-wide fan-out wave
    (wave 1), and a 4-node convergence tail (wave 2) — 20 open topics total.
    One topic ("fan-root") gets 2 member tasks with a dep edge for drill-in."""
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


@pytest.mark.asyncio
async def test_wave_bands_and_critical_rail_render(tmp_path):
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FrontierScreen)
        text = screen._body_text
        assert "wave 1" in text and "wave 2" in text
        assert "possible" in text and "slots free" in text
        assert "critical rail" in text
        assert screen._layout.critical_path  # a chain exists (root -> wide -> tail)


@pytest.mark.asyncio
async def test_wide_fan_in_does_not_crash(tmp_path):
    """No crash on wide fan-in — carries forward the 2026-07-02 gitlog-view
    IndexError incident onto the FrontierScreen wiring (layout-engine math
    itself is pinned in tests/test_frontier_layout.py)."""
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(25):
            await pilot.press("j")
        await pilot.pause()
        assert isinstance(app.screen, FrontierScreen)  # still alive, no crash


@pytest.mark.asyncio
async def test_drill_in_and_back_out(tmp_path):
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        # Selection starts on the first selectable row: fan-root (wave 0).
        assert screen._selectable(screen._layout)[screen._sel].id == "fan-root"
        await pilot.press("enter")
        await pilot.pause()
        assert screen._drill_topic_id == "fan-root"
        # task-a is verified -> collapses into the trunk stub; task-b is open.
        assert "1 verified topic" in screen._body_text
        assert "Task B" in screen._body_text
        await pilot.press("escape")
        await pilot.pause()
        assert screen._drill_topic_id is None
        assert isinstance(app.screen, FrontierScreen)  # escape backed out, not closed


@pytest.mark.asyncio
async def test_wave_fold_hides_rows_behind_summary(tmp_path):
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        await pilot.press("j")  # fan-root -> runner (still wave 0)
        await pilot.press("j")  # runner -> first wave-1 row
        await pilot.pause()
        assert screen._selectable(screen._layout)[screen._sel].wave == 1
        await pilot.press("z")
        await pilot.pause()
        assert "folded" in screen._body_text
        assert not any(
            r.id.startswith("w") for r in screen._layout.rows
            if r.kind not in ("wave-band", "fold-summary")
        )
        await pilot.press("z")
        await pilot.pause()
        assert "Wide 0" in screen._body_text


@pytest.mark.asyncio
async def test_tab_cycles_projects(tmp_path):
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    _second_project(db)
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen._dag.project_id == "P"
        await pilot.press("tab")
        await pilot.pause()
        assert screen._dag.project_id == "P2"
        assert "Only Topic" in screen._body_text


@pytest.mark.asyncio
async def test_detail_pane_updates_on_navigation(tmp_path):
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        first_detail = screen._detail_text
        assert "fan-root" in first_detail
        await pilot.press("j")
        await pilot.pause()
        assert screen._detail_text != first_detail


@pytest.mark.asyncio
async def test_running_row_shows_agent_meta_wide_hides_it_narrow(tmp_path):
    """Narrow-viewport degradation (fr-smoke): agent info is the first thing
    dropped from a running row at reduced width; elapsed stays."""
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app = _Harness()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert "coder·sonnet" in screen._body_text
        assert "0m" in screen._body_text

    app2 = _Harness()
    async with app2.run_test(size=(60, 40)) as pilot:
        await pilot.pause()
        screen2 = app2.screen
        assert "coder·sonnet" not in screen2._body_text
        assert "0m" in screen2._body_text


@pytest.mark.asyncio
async def test_detail_pane_collapses_on_short_viewport(tmp_path):
    from textual.app import App
    from juggle_cockpit_frontier_screen import FrontierScreen, MIN_DETAIL_PANE_HEIGHT

    db = _fixture_20_node(str(tmp_path / "juggle.db"))
    dags = _dags(db)

    class _Harness(App):
        def on_mount(self):
            self.push_screen(FrontierScreen(dags, "P", db))

    app_tall = _Harness()
    async with app_tall.run_test(size=(120, MIN_DETAIL_PANE_HEIGHT + 5)) as pilot:
        await pilot.pause()
        assert app_tall.screen.query_one("#frontier-detail").display is True

    app_short = _Harness()
    async with app_short.run_test(size=(120, MIN_DETAIL_PANE_HEIGHT - 2)) as pilot:
        await pilot.pause()
        assert app_short.screen.query_one("#frontier-detail").display is False


@pytest.mark.asyncio
async def test_L_and_G_both_open_frontier_screen(tmp_path):
    """Post plan-view rebind (2026-07-02): the Frontier railroad moved to `L`
    (uppercase, literal — the shift+letter lesson); `G` still opens it for
    muscle memory. Lowercase `l` now opens the Plan view (see
    tests/test_plan_screen.py)."""
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_frontier_screen import FrontierScreen
    from dbops import db_graph as g
    from juggle_graph_dispatch import ARMED_PROJECT_KEY
    from juggle_db import JuggleDB

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    with db._connect() as conn:
        _project(conn, "P", "Proj")
        conn.commit()
    g.create_task(db, task_id="n1", project_id="P", title="One", prompt="do 1")
    db.set_setting(ARMED_PROJECT_KEY, "P")

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.1)
        await pilot.press("L")
        await pilot.pause(0.15)
        assert isinstance(app.screen, FrontierScreen)
        await pilot.press("q")
        await pilot.pause(0.1)

    app2 = CockpitApp(db_path=db_path)
    async with app2.run_test(size=(160, 40)) as pilot:
        await pilot.press("G")
        await pilot.pause(0.15)
        assert isinstance(app2.screen, FrontierScreen)
