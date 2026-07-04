"""F2 reuse guard: graph_tick never dispatches two topics into one thread
(2026-07-04 dual-dispatch-binding RCA, defense-in-depth for a stale DB).

Incident 2026-07-03: three topics shared one conversation b28610a6 as their
dispatch thread; graph_tick reused that single thread per topic, putting
multiple coders in one worktree /tmp/juggle-juggle-GP (corruption).

F1 (record_surfacing_conversation) prevents the fan-in binding at add-task time,
but a DB that ALREADY contains a fan-in binding (pre-fix rows) is not healed by
F1. F2 makes graph_tick defensive: it refuses to reuse a thread that is already
the dispatch thread of another ACTIVE (dispatching/running/integrating) topic and
mints a fresh thread instead — which also rebinds the colliding topic onto its own
thread (self-heal).

RED pre-fix: both topics dispatch onto the shared conversation thread.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


class _FakeDispatch:
    def __init__(self):
        self.calls = []  # (thread_id, topic_id)

    def __call__(self, db, thread_id, prompt, task):
        self.calls.append((thread_id, task["id"]))


def test_tick_does_not_reuse_a_thread_active_on_another_topic(db):
    # A stale fan-in binding: one conversation is the dispatch thread of BOTH
    # topics (the exact pre-fix corruption state).
    convo = db.create_thread("shared conversation surface", session_id="s",
                             project_id="INBOX")
    for tid in ("T-alpha", "T-beta"):
        tp.create_topic(db, topic_id=tid, project_id="INBOX", title=tid)
        nid = f"{tid}-k0"
        g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
        g.set_task_topic(db, nid, tid)
        tp.set_topic_thread(db, tid, convo)  # both bound to the SAME thread
    tp.recompute_topic_ready(db, "INBOX")
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")

    fake = _FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)

    # Both topics dispatched...
    assert {tid for _, tid in fake.calls} == {"T-alpha", "T-beta"}
    # ...but into TWO DISTINCT threads — never both onto the shared conversation.
    threads = [th for th, _ in fake.calls]
    assert len(set(threads)) == 2, f"topics collided on one thread: {fake.calls}"
    # The colliding topic was rebound onto its own fresh thread (self-heal): the
    # two topics no longer share a dispatch thread.
    assert tp.get_topic(db, "T-alpha")["thread_id"] != tp.get_topic(db, "T-beta")["thread_id"]
