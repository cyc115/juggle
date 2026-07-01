"""Surface-B RailroadScreen tests (2026-06-30 graph railroad, T5)."""
import pytest

from juggle_cockpit_graph_lanes import assign_lanes  # noqa: F401  (import path guard)
from juggle_cockpit_railroad import node_detail_text, RailroadScreen


def test_node_detail_text_has_core_fields(juggle_db):
    """2026-06-30 graph railroad: detail pane surfaces id/state/verify."""
    from dbops import db_graph

    db_graph.create_task(
        juggle_db, task_id="t1", project_id="P", title="T", prompt="p", verify_cmd="pytest"
    )
    txt = node_detail_text(juggle_db, "t1")
    assert "t1" in txt and "pytest" in txt and "state" in txt.lower()


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
async def test_railroad_screen_navigation(juggle_db):
    """2026-06-30 graph railroad: j moves the cursor, detail follows, q pops back."""
    from textual.app import App

    _seed(juggle_db)
    dags = _dags_for(juggle_db)
    assert dags, "expected a seeded project DAG"
    db = juggle_db

    class _Harness(App):
        def on_mount(self) -> None:
            self.push_screen(RailroadScreen(dags, dags[0].project_id, db))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RailroadScreen)
        start = screen._sel
        assert screen._lines()[start].id in screen._detail_text
        await pilot.press("j")
        await pilot.pause()
        assert screen._sel == start + 1
        assert screen._lines()[screen._sel].id in screen._detail_text
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, RailroadScreen)
