"""Cockpit Topics-pane nesting (2026-06-30 topic-graph-state-unify F7).

Child task-nodes render indented under their conversation topic; in-progress
topics expand, done topics collapse to a rollup.
"""
from dbops import db_graph
from juggle_cockpit_model import snapshot
from juggle_cockpit_view import render_topics


_RUNNING = ("deps_ready", "claim", "dispatch")
_VERIFIED = (*_RUNNING, "integrate_start", "integrate_ok")


def _topic_with_children(db, *, done):
    topic = db.create_thread(topic="build login page", session_id="s")
    db.add_message(topic, role="user", content="build it")
    # A done topic has all children merged-terminal; an in-progress one keeps a
    # running child alongside a verified one.
    plan = (("k1", _VERIFIED), ("k2", _VERIFIED)) if done else (
        ("k1", _RUNNING), ("k2", _VERIFIED)
    )
    for cid, evs in plan:
        db_graph.create_task(db, task_id=cid, project_id="INBOX", title=cid, prompt="p")
        db_graph.set_task_topic(db, cid, topic)
        for ev in evs:
            db_graph.task_transition(db, cid, ev)
    if done:
        db.set_thread_status(topic, "closed")
    return topic


def _plain(panel):
    from rich.console import Console

    con = Console(width=80, no_color=True)
    with con.capture() as cap:
        con.print(panel)
    return cap.get()


def test_snapshot_carries_children(juggle_db):
    topic = _topic_with_children(juggle_db, done=False)
    state = snapshot(juggle_db)
    t = next(t for t in state.topics if t.id == topic)
    assert len(t.children) == 2
    assert {c.state for c in t.children} == {"running", "verified"}


def test_expanded_topic_shows_child_ids(juggle_db):
    _topic_with_children(juggle_db, done=False)
    state = snapshot(juggle_db)
    panel = render_topics(state.topics, bp="wide")
    text = _plain(panel)
    assert "k1" in text and "k2" in text


def test_done_topic_collapses_to_rollup(juggle_db):
    _topic_with_children(juggle_db, done=True)
    state = snapshot(juggle_db)
    panel = render_topics(state.topics, bp="wide")
    text = _plain(panel)
    assert "2/2 done" in text
    assert "k1" not in text  # collapsed — individual child ids hidden
