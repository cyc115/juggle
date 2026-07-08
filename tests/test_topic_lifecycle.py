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


# ── reconcile_adhoc_integrate (2026-07-07 #5558/#5564) ──────────────────────
#
# An ad-hoc (chat-dispatched) thread has no kind='topic' node, so integrate's
# _record_merged_sha topic lookup is a no-op — nothing else reconciled its
# conversation node's state, leaving it 'background' forever after the work
# landed. reconcile_adhoc_integrate is the seam integrate calls on a
# successful landed submit for such a thread.


def test_reconcile_adhoc_integrate_closes_background_thread(juggle_db):
    tid = juggle_db.create_thread(topic="agent work", session_id="s")
    juggle_db.set_conversation_background(tid)

    lc.reconcile_adhoc_integrate(juggle_db, tid, "deadbeef")

    after = juggle_db.get_thread(tid)
    assert after["state"] != "background"
    assert after["merged_sha"] == "deadbeef"


def test_reconcile_adhoc_integrate_idempotent_on_already_terminal_node(juggle_db):
    """Integrate can re-run (idempotent pipeline) — reconciling an
    already-terminal node must be a no-op, not raise or flip state back."""
    tid = juggle_db.create_thread(topic="agent work", session_id="s")
    juggle_db.set_conversation_background(tid)

    lc.reconcile_adhoc_integrate(juggle_db, tid, "deadbeef")
    first = juggle_db.get_thread(tid)

    lc.reconcile_adhoc_integrate(juggle_db, tid, "deadbeef")
    second = juggle_db.get_thread(tid)

    assert second["state"] == first["state"]
    assert second["merged_sha"] == first["merged_sha"]
