"""Phase-4 boundary #2 end-to-end pins (loop-entity V1, 2026-07-04).

Phase 3 left the CALLER `mark_graph_topic` computing `all_verified =
all(state=='verified')` and treating any non-'verified' terminal as a FAILURE
(else-branch: propagate_topic_failure + bogus 'topic failed' HIGH item). Deliver
loops did not exist yet. Now they do: a single-topic delivery='deliver' loop's
member tasks reach 'delivered' (a success terminal), NOT 'verified'.

Incident pinned (boundary #2): without the fix, a deliver loop driven through the
REAL completion path (`mark_graph_topic`) passes verify_ok=False → mark_topic_
completion walks verify_fail → the topic wedges at 'failed-verify' and a bogus
failure is propagated → the loop iteration never reaches a success terminal →
overlap-skip then kills the loop forever (LEAD FINDING). The completion caller must
gate on the success-terminal SET so a deliver topic reaches 'delivered'.

RED on pre-Phase-4 code: `mark_graph_topic` gates on the 'verified' literal, so the
deliver topic lands in 'failed-verify', not 'delivered'.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
from juggle_cmd_agents_graph_topics import mark_graph_topic  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "loop-deliver.db"))
    d.init_db()
    return d


def _topic(db, tid, delivery):
    t.create_topic(db, topic_id=tid, project_id="INBOX", title=f"Topic {tid}")
    with db._connect() as c:
        c.execute("UPDATE nodes SET delivery=? WHERE id=? AND kind='topic'",
                  (delivery, tid))
        c.commit()


def _member_task_to(db, nid, topic_id, final_event):
    """Create a member task and drive it to its success terminal via the machine."""
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt=f"do {nid}")
    g.set_task_topic(db, nid, topic_id)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", final_event):
        g.task_transition(db, nid, ev)


def _bound_thread(db, topic_id):
    thread_id = db.create_thread(topic="loop-iter", session_id="s")
    t.set_topic_thread(db, topic_id, thread_id)
    return thread_id


def test_single_topic_deliver_loop_completes_to_delivered(db):
    """MANDATORY (boundary #2). A single-topic delivery='deliver' loop, driven
    through the REAL completion caller `mark_graph_topic`, reaches 'delivered' —
    NOT stuck pre-verified, NOT wedged at 'failed-verify'."""
    _topic(db, "D1", "deliver")
    _member_task_to(db, "d-task", "D1", "deliver_ok")  # member task → 'delivered'
    assert t.get_topic(db, "D1")["state"] == "open"
    thread_id = _bound_thread(db, "D1")

    mark_graph_topic(db, thread_id, integrate_ok=True, handoff="digest ready",
                     session_id="s", topic_id="D1")

    topic = t.get_topic(db, "D1")
    assert topic["state"] == "delivered"
    assert not topic.get("merged_sha")  # deliver never forges a merge proof


def test_deliver_loop_completion_files_no_failure_item(db):
    """boundary #2 regression: a delivered topic must NOT trip the failure branch
    (which would file a bogus 'topic failed (delivered)' HIGH action item)."""
    _topic(db, "D2", "deliver")
    _member_task_to(db, "d2-task", "D2", "deliver_ok")
    thread_id = _bound_thread(db, "D2")

    mark_graph_topic(db, thread_id, integrate_ok=True, handoff="ok",
                     session_id="s", topic_id="D2")

    failures = [i for i in db.get_open_action_items()
                if (i.get("type") == "failure") and "D2" in (i.get("message") or "")]
    assert failures == []


def test_single_topic_merge_loop_still_completes_to_verified(db, monkeypatch):
    """The merge equivalent is UNCHANGED: a delivery='merge' loop whose member
    tasks are 'verified' reaches 'verified' through the same caller (merged_sha
    gate faked green here — the gate itself is exercised by test_delivered_
    terminal). Proves the success-terminal broadening did not break merge."""
    monkeypatch.setattr(t, "_verified_allowed", lambda db_, tid: True)
    _topic(db, "M1", "merge")
    _member_task_to(db, "m-task", "M1", "integrate_ok")  # member task → 'verified'
    thread_id = _bound_thread(db, "M1")

    mark_graph_topic(db, thread_id, integrate_ok=True, handoff="landed",
                     session_id="s", topic_id="M1")

    assert t.get_topic(db, "M1")["state"] == "verified"
