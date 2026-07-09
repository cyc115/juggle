"""D2 (2026-07-09 incident, follow-up to 190739f/5e0c8f8): graph_tick must
never claim/re-dispatch an ad-hoc thread a BUSY agent is actively working —
even when a tick-minted dispatch title happens to lexically match it.

Incident: an orchestrator ad-hoc thread (created via `thread create` +
`agent send-task`, no db_graph task / db_topics topic binding — i.e. ad-hoc
by the SAME discriminator as juggle_cmd_agents_graph.close_adhoc_run and
juggle_topic_lifecycle.reconcile_adhoc_integrate) had a LIVE agent mid-flight
on it in project INBOX. A later graph_tick, dispatching an unrelated graph
topic whose minted title (``f"[{tid}] {topic['title']}"``) shared 2+
significant content words with the ad-hoc thread's own title, was reused by
db.create_thread's lexical dedup guard (dbops.threads._find_duplicate_open_
thread) — which excluded threads OWNING a *different* active topic/task, but
NOT a thread merely occupied by a busy agent with no topic binding at all.
The tick then bound + re-dispatched INTO the ad-hoc thread: retitling it,
sending the tick's task prompt into an agent already mid-flight on unrelated
ad-hoc work (run misattribution, run 468 / agent 33a87e2b in the real
incident).

Note: an IDLE ad-hoc thread remains a legitimate reuse target by design (the
2026-06-15 test_thread_dedup.py::test_dispatch_reuses_existing_open_thread
intent) — only a BUSY-agent-occupied thread must never be hijacked.

RED pre-fix: the tick's dispatch lands on the busy ad-hoc thread instead of a
fresh one, and the ad-hoc thread gets topic-bound.
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


def test_tick_does_not_hijack_a_lexically_similar_adhoc_thread(db):
    # The orchestrator's own ad-hoc conversation — no db_graph task and no
    # db_topics topic binding (the ad-hoc discriminator's definition) — with a
    # BUSY agent actively mid-flight on it (run 468's agent 33a87e2b in the
    # real incident).
    adhoc_id = db.create_thread(
        "stall nudge false positive investigation", session_id="s",
        project_id="INBOX",
    )
    db.set_conversation_background(adhoc_id)
    agent_id = db.create_agent(role="researcher", pane_id="p0")
    db.update_agent(agent_id, status="busy", assigned_thread=adhoc_id)

    # An unrelated graph topic whose tick-minted dispatch title
    # (f"[{tid}] {title}") shares significant content words with the ad-hoc
    # thread's title above (only action verbs/stopwords differ).
    tid = "T-fix-stall-nudge-false-positive"
    tp.create_topic(db, topic_id=tid, project_id="INBOX", title="stall nudge false positive")
    nid = f"{tid}-k0"
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
    g.set_task_topic(db, nid, tid)
    tp.recompute_topic_ready(db, "INBOX")

    fake = _FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)

    assert fake.calls, "topic was never dispatched"
    dispatched_thread = fake.calls[0][0]
    assert dispatched_thread != adhoc_id, (
        "tick hijacked the ad-hoc thread as its dispatch surface"
    )
    # The ad-hoc thread must remain unbound and untouched by the tick.
    assert tp.get_topic_by_thread(db, adhoc_id) is None
    adhoc_after = db.get_thread(adhoc_id)
    assert adhoc_after["title"] == "stall nudge false positive investigation"
