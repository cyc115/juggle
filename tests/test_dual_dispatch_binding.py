"""Fan-in guard on the dispatch-thread binding (2026-07-04 dual-dispatch-binding RCA).

Incident 2026-07-03: three synthetic graph-topics (T-gp-spine, T-gp-refactor,
T-gp-edit) all bound their kind='dispatch' node_edge to ONE conversation
b28610a6 (user_label GP). graph_tick then reused that single thread per topic,
dispatching multiple coders into the same worktree /tmp/juggle-juggle-GP
(cyc_GP) — a concurrent-write corruption (one coder aborted+stashed).

Root cause: N `add-task --topic GP` calls each synthesize a distinct T-<id>
topic (resolve_dispatch_topic) and each bound the SAME conversation as its
surfacing/dispatch thread — bind_dispatch_thread enforces uniqueness only on
node_id (topic->thread), never on depends_on_id (the thread).

F1 fix: record_surfacing_conversation refuses to bind a conversation that is
already ANOTHER topic's dispatch thread — the first topic keeps the surfacing
binding; later topics stay unbound so graph_tick mints a fresh mirror thread.
RED pre-fix: the second topic also resolves thread_id == the GP conversation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
from juggle_graph_add_surfacing import record_surfacing_conversation  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def test_two_topics_naming_one_conversation_do_not_share_dispatch_thread(db):
    # A single human conversation labeled "GP".
    conv = db.create_thread("GP work", session_id="s", project_id="INBOX")
    db.update_thread(conv, user_label="GP")
    assert db.get_thread_by_user_label("GP")["id"] == conv  # resolver sanity

    # Two tasks added with `--topic GP` each synthesize a distinct T-<id> topic.
    for tid in ("T-gp-spine", "T-gp-refactor"):
        tp.create_topic(db, topic_id=tid, project_id="INBOX", title=tid)
        record_surfacing_conversation(db, tid, "GP")

    # The FIRST topic keeps the surfacing binding.
    assert tp.get_topic(db, "T-gp-spine")["thread_id"] == conv
    # The SECOND must NOT reuse the same dispatch thread (the collision).
    assert tp.get_topic(db, "T-gp-refactor")["thread_id"] != conv


def test_rebinding_same_topic_is_idempotent(db):
    # Re-running add-task for the SAME topic must still (re)bind it — the guard
    # only refuses a DIFFERENT topic, never the owner rebinding itself.
    conv = db.create_thread("GP work", session_id="s", project_id="INBOX")
    db.update_thread(conv, user_label="GP")
    tp.create_topic(db, topic_id="T-gp-spine", project_id="INBOX", title="spine")

    record_surfacing_conversation(db, "T-gp-spine", "GP")
    record_surfacing_conversation(db, "T-gp-spine", "GP")

    assert tp.get_topic(db, "T-gp-spine")["thread_id"] == conv
