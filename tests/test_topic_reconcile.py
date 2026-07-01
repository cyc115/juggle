"""Derived-close reconciler pins (2026-06-30 topic-graph-state-unify F3).

Topic state is derived from child task states + idle-since-human-message. The
reconciler is deterministic under injected now/close_idle_min.
"""
from datetime import datetime, timedelta, timezone

import juggle_topic_reconcile as tr
from dbops import db_graph

_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)

_TO_STATE = {
    "running": ("deps_ready", "claim", "dispatch"),
    "verified": ("deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"),
    "failed-exec": ("deps_ready", "claim", "dispatch", "exec_fail"),
}


def _drive(db, task_id, target):
    for ev in _TO_STATE[target]:
        db_graph.task_transition(db, task_id, ev)


def _mk_topic_with_child(db, *, child_state, human_msg_age_min=None, now=_NOW, cid="c1"):
    topic = db.create_thread(topic="feature", session_id="s")
    if human_msg_age_min is not None:
        db.add_message(topic, role="user", content="build the thing")
        ts = (now - timedelta(minutes=human_msg_age_min)).isoformat()
        with db._connect() as c:
            c.execute(
                "UPDATE messages SET created_at=? WHERE thread_id=? AND role='user'",
                (ts, topic),
            )
            c.commit()
    db_graph.create_task(db, task_id=cid, project_id="INBOX", title="t", prompt="p")
    db_graph.set_task_topic(db, cid, topic)
    _drive(db, cid, child_state)
    return topic


def test_children_merged_and_idle_closes(juggle_db):
    """2026-06-30 unify: all children verified + idle 40m -> topic done."""
    topic = _mk_topic_with_child(juggle_db, child_state="verified", human_msg_age_min=40)
    changed = tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert (topic, "open", "done") in changed
    assert juggle_db.get_thread(topic)["state"] == "done"


def test_merged_but_recent_message_stays_open(juggle_db):
    """2026-06-30 unify: merged child but human msg 5m ago -> idle guard, stays open."""
    topic = _mk_topic_with_child(juggle_db, child_state="verified", human_msg_age_min=5)
    tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert juggle_db.get_thread(topic)["state"] != "done"


def test_childless_human_topic_not_closed(juggle_db):
    """2026-06-30 unify: a human-facing topic with NO children is never auto-closed."""
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.add_message(topic, role="user", content="idea")
    tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert juggle_db.get_thread(topic)["state"] != "done"


def test_done_topic_reopens_when_child_recent(juggle_db):
    """2026-06-30 unify: derive reopens a done topic when the idle guard trips."""
    topic = _mk_topic_with_child(juggle_db, child_state="verified", human_msg_age_min=1)
    juggle_db.set_thread_status(topic, "closed")  # simulate previously derived-done
    tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert juggle_db.get_thread(topic)["state"] == "open"


def test_running_child_keeps_open(juggle_db):
    """2026-06-30 unify: an in-flight child keeps the topic open."""
    topic = _mk_topic_with_child(juggle_db, child_state="running", human_msg_age_min=99)
    tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert juggle_db.get_thread(topic)["state"] != "done"


def test_live_agent_blocks_close(juggle_db):
    """2026-06-30 unify G4a: a busy bound agent blocks the derived close."""
    topic = _mk_topic_with_child(juggle_db, child_state="verified", human_msg_age_min=40)
    aid = juggle_db.create_agent(role="coder", pane_id="%1")
    juggle_db.update_agent(aid, assigned_thread=topic, status="busy")
    tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert juggle_db.get_thread(topic)["state"] != "done"


def test_idempotent_second_run_noop(juggle_db):
    """2026-06-30 unify: reconcile is idempotent — second run changes nothing."""
    _mk_topic_with_child(juggle_db, child_state="verified", human_msg_age_min=40)
    tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    again = tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert again == []


def test_close_idle_min_env_default(monkeypatch):
    monkeypatch.delenv("JUGGLE_TOPIC_CLOSE_IDLE_MIN", raising=False)
    assert tr.close_idle_min() == 30
    monkeypatch.setenv("JUGGLE_TOPIC_CLOSE_IDLE_MIN", "5")
    assert tr.close_idle_min() == 5
