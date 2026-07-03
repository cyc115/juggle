"""node_detail_text pin — extracted from the removed RailroadScreen (Surface-B,
2026-06-30 graph railroad, T5) so the Frontier Railroad (fr-screen) can reuse
it for its bottom node-detail pane. Only the Screen class was removed."""
from juggle_cockpit_node_detail import node_detail_text


def test_node_detail_text_has_core_fields(juggle_db):
    """2026-06-30 graph railroad: detail pane surfaces id/state/verify."""
    from dbops import db_graph

    db_graph.create_task(
        juggle_db, task_id="t1", project_id="P", title="T", prompt="p", verify_cmd="pytest"
    )
    txt = node_detail_text(juggle_db, "t1")
    assert "t1" in txt and "pytest" in txt and "state" in txt.lower()
