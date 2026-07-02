"""Surface-E GitlogScreen tests (cockpit-gitlog-view)."""
import pytest

from juggle_cockpit_gitlog_screen import GitlogScreen


def _seed(db):
    from dbops import db_graph

    db_graph.create_task(db, task_id="a", project_id="P", title="Alpha", prompt="pa", verify_cmd="va")
    db_graph.create_task(db, task_id="b", project_id="P", title="Beta", prompt="pb", verify_cmd="vb")
    from dbops.db_graph_edges import replace_edges

    replace_edges(db, "b", ["a"])


def _dags_for(db):
    from juggle_cockpit_graph_dag import load_graph_dags

    with db._connect() as conn:
        return load_graph_dags(conn)


@pytest.mark.asyncio
async def test_gitlog_screen_navigation_and_close(juggle_db):
    from textual.app import App

    _seed(juggle_db)
    dags = _dags_for(juggle_db)
    assert dags, "expected a seeded project DAG"
    db = juggle_db

    class _Harness(App):
        def on_mount(self) -> None:
            self.push_screen(GitlogScreen(dags, db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, GitlogScreen)
        start = screen._sel
        await pilot.press("j")
        await pilot.pause()
        assert screen._sel == start + 1
        await pilot.press("l")
        await pilot.pause()
        assert not isinstance(app.screen, GitlogScreen)


@pytest.mark.asyncio
async def test_gitlog_screen_toggle_from_graph_mode(juggle_db):
    """'l' inside graph mode opens the full-screen view; same key closes it."""
    from juggle_cockpit import CockpitApp
    from dbops import db_graph as g
    from juggle_graph_dispatch import ARMED_PROJECT_KEY
    from datetime import datetime, timezone

    juggle_db.set_active(True)
    now = datetime.now(timezone.utc).isoformat()
    with juggle_db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", ("P", "Proj", "active", now, now),
        )
        conn.commit()
    g.create_task(juggle_db, task_id="n1", project_id="P", title="One", prompt="do 1")
    juggle_db.set_setting(ARMED_PROJECT_KEY, "P")

    app = CockpitApp(db_path=juggle_db.db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("g")
        await pilot.pause()
        assert app._graph_mode is True
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, GitlogScreen)
        await pilot.press("l")
        await pilot.pause()
        assert not isinstance(app.screen, GitlogScreen)
