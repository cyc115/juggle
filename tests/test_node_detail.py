"""node_detail_text / topic_detail_text pins — extracted from the removed
RailroadScreen (Surface-B, 2026-06-30 graph railroad, T5) so the Frontier
Railroad (fr-screen) can reuse them for its bottom node-detail pane.
topic_detail_text is new (fr-vf-polish, 2026-07-03) — see the regression pin
below for the bug it fixes."""
from datetime import datetime, timezone

from juggle_cockpit_node_detail import node_detail_text, topic_detail_text


def _now():
    return datetime.now(timezone.utc).isoformat()


def _project(conn, pid):
    now = _now()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at,last_active) VALUES(?,?,?,?,?)",
        (pid, "Proj", "active", now, now),
    )


def _topic(conn, tid, pid, title, state="open", parent_id=None):
    now = _now()
    conn.execute(
        "INSERT INTO nodes(id,kind,title,objective,state,project_id,parent_id,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (tid, "topic" if parent_id is None else "task", title, "", state, pid,
         parent_id, now, now),
    )


def test_node_detail_text_has_core_fields(juggle_db):
    """2026-06-30 graph railroad: detail pane surfaces id/state/verify."""
    from dbops import db_graph

    db_graph.create_task(
        juggle_db, task_id="t1", project_id="P", title="T", prompt="p", verify_cmd="pytest"
    )
    txt = node_detail_text(juggle_db, "t1")
    assert "t1" in txt and "pytest" in txt and "state" in txt.lower()


def test_node_detail_text_escapes_literal_brackets_in_freeform_fields():
    """markup=True Statics interpret "[...]" -- a task id/title/prompt that
    happens to contain literal brackets (e.g. "[CR] fix the thing") must never
    be swallowed as a markup tag once rendered."""
    from juggle_frontier_rails import style

    escaped = style("[CR] fix the thing")
    assert escaped == "\\[CR] fix the thing"  # "\[" -- Rich renders the literal "["


def test_topic_detail_text_never_blank_at_topic_tier(juggle_db):
    """Regression pin (2026-07-03): selecting a TOPIC row used to call
    node_detail_text (which only resolves kind='task' ids via
    db_graph.get_task), so the pane rendered blank title/state fields for
    every topic-tier selection. topic_detail_text must show real values."""
    with juggle_db._connect() as conn:
        _project(conn, "P")
        _topic(conn, "irl-backbone", "P", "IRL Backbone", state="running")
        _topic(conn, "sub-a", "P", "Sub A", state="verified", parent_id="irl-backbone")
        _topic(conn, "sub-b", "P", "Sub B", state="open", parent_id="irl-backbone")
        conn.commit()

    txt = topic_detail_text(juggle_db, "irl-backbone")
    assert "title" in txt.lower() and "<blank>" not in txt
    assert "IRL Backbone" in txt
    assert "running" in txt
    assert "1/2" in txt  # one of two member tasks verified
    assert "Sub A" in txt and "Sub B" in txt


def test_topic_detail_text_falls_back_gracefully_for_unknown_id(juggle_db):
    txt = topic_detail_text(juggle_db, "no-such-id")
    assert "(none)" in txt
    assert "title" in txt.lower()
