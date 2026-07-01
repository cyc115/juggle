"""Event-driven close/reopen triggers (2026-06-30 topic-graph-state-unify F4)."""
from dbops import db_graph


def test_human_message_reopens_done_topic(juggle_db):
    """2026-06-30 unify: add_message from a human reopens a done conversation topic."""
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.set_thread_status(topic, "closed")  # done
    juggle_db.add_message(topic, role="user", content="one more thing please")
    assert juggle_db.get_thread(topic)["state"] == "open"


def test_task_notification_does_not_reopen(juggle_db):
    """2026-06-30 unify: junk/auto message must NOT reopen a done topic."""
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.set_thread_status(topic, "closed")
    juggle_db.add_message(topic, role="user", content="# Autonomous loop tick")
    assert juggle_db.get_thread(topic)["state"] == "done"


def test_assistant_message_does_not_reopen(juggle_db):
    """2026-06-30 unify: an assistant message never reopens a done topic."""
    topic = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.set_thread_status(topic, "closed")
    juggle_db.add_message(topic, role="assistant", content="here is the answer")
    assert juggle_db.get_thread(topic)["state"] == "done"


def test_child_verified_event_closes_idle_parent(juggle_db):
    """2026-06-30 unify: marking the bound child verified reconciles the parent toward close."""
    import juggle_cmd_agents_graph as g

    topic = juggle_db.create_thread(topic="feature", session_id="s")
    agent_thread = juggle_db.create_thread(topic="agent", session_id="s")
    # A synthetic child bound to the agent thread, parented to the topic, running.
    import juggle_topic_lifecycle as lc

    lc.ensure_topic_child(
        juggle_db, topic_id=topic, agent_thread_id=agent_thread, prompt="do it"
    )
    # No human message on the topic → idle is None → merged child should close it.
    g.mark_graph_task(juggle_db, agent_thread, integrate_ok=True, handoff=None, session_id="s")
    assert db_graph.get_task_by_thread(juggle_db, agent_thread)["state"] == "verified"
    assert juggle_db.get_thread(topic)["state"] == "done"
