"""Invariant pins for the unified topic/graph state tracker
(2026-06-30 topic-graph-state-unify F8)."""
from datetime import datetime, timedelta, timezone

import juggle_topic_lifecycle as lc
import juggle_topic_reconcile as tr
from dbops import db_graph

_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def test_synthetic_child_never_auto_dispatched(juggle_db):
    """2026-06-30 unify OQ-3: a synthetic forward-link child is created in
    'running' (never 'ready'), so graph_tick — which claims READY nodes — never
    auto-dispatches it. This is the invariant that keeps ad-hoc chat work off the
    autopilot. (graph_tick DOES dispatch a ready kind='task'; the guarantee is the
    child's entry state, not the tick ignoring tasks.)"""
    from juggle_graph_dispatch import graph_tick

    topic = juggle_db.create_thread(topic="feature", session_id="s")
    agent_thread = juggle_db.create_thread(topic="agent", session_id="s")
    cid = lc.ensure_topic_child(
        juggle_db, topic_id=topic, agent_thread_id=agent_thread, prompt="do it"
    )
    assert db_graph.get_task(juggle_db, cid)["state"] == "running"

    dispatched = []
    graph_tick(juggle_db, dispatch_fn=lambda *a, **k: dispatched.append(str(a)))
    # Not claimed (it was never 'ready') and not reset (freshly-updated in-flight).
    assert not any(cid in d for d in dispatched)
    assert db_graph.get_task(juggle_db, cid)["state"] == "running"


def test_reconcile_second_run_is_noop(juggle_db):
    """2026-06-30 unify: reconcile_conversation_topics is idempotent."""
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.add_message(topic, role="user", content="build the thing")
    ts = (_NOW - timedelta(minutes=99)).isoformat()
    with juggle_db._connect() as c:
        c.execute(
            "UPDATE messages SET created_at=? WHERE thread_id=? AND role='user'",
            (ts, topic),
        )
        c.commit()
    db_graph.create_task(juggle_db, task_id="c1", project_id="INBOX", title="t", prompt="p")
    db_graph.set_task_topic(juggle_db, "c1", topic)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"):
        db_graph.task_transition(juggle_db, "c1", ev)
    first = tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert first  # closed on the first pass
    second = tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert second == []


def test_childless_feature_topic_not_closed_by_either_path(juggle_db):
    """2026-06-30 unify: the 2026-06-21 anti-hijack fix survives — a childless
    human-facing topic is closed by NEITHER the reconciler NOR decide_thread_close."""
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.add_message(topic, role="user", content="here is a real idea to build")
    # Reconciler: no children → derive None → unchanged.
    tr.reconcile_conversation_topics(juggle_db, now=_NOW, close_idle_min=30)
    assert juggle_db.get_thread(topic)["state"] != "done"
    # decide_thread_close: human message, not in-flight → None (leave untouched).
    thread = juggle_db.get_thread(topic)
    assert lc.decide_thread_close(juggle_db, thread, topic) is None
