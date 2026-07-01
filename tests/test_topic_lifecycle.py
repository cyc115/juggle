"""Tests for juggle_topic_lifecycle (2026-06-30 topic-graph-state-unify).

R1: decide_thread_close is the pure close/preserve decision extracted from
juggle_cmd_agents_complete — it must reproduce the 2026-06-21 anti-hijack
behavior exactly.
"""
import juggle_topic_lifecycle as lc


def test_decide_thread_close_no_human_message_closes(juggle_db):
    tid = juggle_db.create_thread(topic="agent work", session_id="s")
    thread = juggle_db.get_thread(tid)
    assert lc.decide_thread_close(juggle_db, thread, tid) == "closed"


def test_decide_thread_close_human_inflight_reactivates(juggle_db):
    tid = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.add_message(tid, role="user", content="please build the login page")
    juggle_db.set_thread_status(tid, "running")
    thread = juggle_db.get_thread(tid)
    assert lc.decide_thread_close(juggle_db, thread, tid) == "active"


def test_decide_thread_close_human_terminal_untouched(juggle_db):
    tid = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.add_message(tid, role="user", content="please build the login page")
    juggle_db.set_thread_status(tid, "closed")
    thread = juggle_db.get_thread(tid)
    assert lc.decide_thread_close(juggle_db, thread, tid) is None
