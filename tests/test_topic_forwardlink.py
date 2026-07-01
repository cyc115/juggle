"""ensure_topic_child forward-link (2026-06-30 topic-graph-state-unify F1).

The root-cause kill: a topic's work becomes a child task-node (parent_id→topic),
so topic state can be DERIVED. Idempotent — graph-first reparents (no duplicate),
ad-hoc creates exactly one running child, re-dispatch is a no-op.
"""
import juggle_topic_lifecycle as lc
from dbops import db_graph


def _task_count(db):
    with db._connect() as c:
        return c.execute("SELECT COUNT(*) FROM nodes WHERE kind='task'").fetchone()[0]


def test_adhoc_creates_one_running_child(juggle_db):
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    agent_thread = juggle_db.create_thread(topic="agent", session_id="s")
    tid = lc.ensure_topic_child(
        juggle_db, topic_id=topic, agent_thread_id=agent_thread, prompt="do it"
    )
    child = db_graph.get_task(juggle_db, tid)
    assert child["topic_id"] == topic and child["state"] == "running"
    assert _task_count(juggle_db) == 1


def test_redispatch_is_idempotent(juggle_db):
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    agent_thread = juggle_db.create_thread(topic="agent", session_id="s")
    a = lc.ensure_topic_child(
        juggle_db, topic_id=topic, agent_thread_id=agent_thread, prompt="do it"
    )
    n = _task_count(juggle_db)
    b = lc.ensure_topic_child(
        juggle_db, topic_id=topic, agent_thread_id=agent_thread, prompt="do it"
    )
    assert a == b and _task_count(juggle_db) == n


def test_graph_first_reparents_no_duplicate(juggle_db):
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    agent_thread = juggle_db.create_thread(topic="agent", session_id="s")
    db_graph.create_task(
        juggle_db, task_id="realtask", project_id="P1", title="t", prompt="p"
    )
    db_graph.set_task_thread(juggle_db, "realtask", agent_thread)
    n = _task_count(juggle_db)
    tid = lc.ensure_topic_child(
        juggle_db, topic_id=topic, agent_thread_id=agent_thread, prompt="ignored"
    )
    assert tid == "realtask"
    assert db_graph.get_task(juggle_db, "realtask")["topic_id"] == topic
    assert _task_count(juggle_db) == n  # no duplicate
