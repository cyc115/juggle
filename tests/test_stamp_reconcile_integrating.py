"""Regression pin — the agent_runs 'completed' stamp MUST also land when a topic
reaches 'integrating' through reconcile_topic_state (the graph_mark_task /
spool-apply completion ingress), not only through mark_topic_integrating
(fix-stamp-spool-apply-path).

LIVE COUNTER-EVIDENCE (2026-07-04 08:30, topics SC SD SE): a coder marked its
last task 'verified' via `graph mark-task`, whose watchdog spool-apply path
(cmd_graph_mark_task → reconcile_topic_state) walks the topic to 'integrating'
by writing state DIRECTLY (db_topics_reconcile.write_state) — a DIFFERENT seam
than mark_topic_integrating. f258871 stamped only the latter, so the run stayed
'dispatched', the reintegrate bound-agent guard blocked, and the operator had to
`agent release --force` (10th manual release of the day).

f258871's own pin passes because it drains an agent_complete event (which flows
through start_detached_integrate → mark_topic_integrating). The live drain path
is graph_mark_task → reconcile, which these pins cover.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops.spool import SpoolEvent
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    import juggle_cmd_agents_common
    import juggle_cmd_graph

    # cmd_graph_mark_task resolves cg.get_db(db_path, init=…) at call time, so the
    # stub must accept the real args — a bare lambda: d raises TypeError there.
    monkeypatch.setattr(common, "get_db", lambda *a, **k: d)
    monkeypatch.setattr(juggle_cmd_agents_common, "get_db", lambda *a, **k: d)
    monkeypatch.setattr(juggle_cmd_graph, "get_db", lambda *a, **k: d)
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return d


def _seed_running_topic(db, thread_id, topic_id, *, task_state="verified"):
    """A topic bound to a thread, in 'running', with a single task in task_state."""
    from dbops import db_graph, db_topics

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    task = f"{topic_id}-t0"
    db_graph.create_task(db, task_id=task, project_id="INBOX", title=task, prompt="x")
    db_graph.set_task_topic(db, task, topic_id)
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='running' WHERE id=? AND kind='topic'",
                  (topic_id,))
        c.execute("UPDATE nodes SET state=? WHERE id=?", (task_state, task))
        c.commit()
    db_topics.set_topic_thread(db, topic_id, thread_id)
    return task


def _bind_busy_agent(db, thread_id):
    """Bind a busy pool agent + open its dispatched ledger run (pre-drain state)."""
    aid = db.create_agent(role="coder", pane_id="p1", repo_path="/repo")
    db.update_agent(aid, status="busy", assigned_thread=thread_id,
                    busy_since=datetime.now(timezone.utc).isoformat())
    db.insert_agent_run(
        thread_id=thread_id, input_prompt="do the thing", agent_id=aid, role="coder",
        model="claude", harness="claude", project_id=None, topic_id=None, task_id=None,
    )
    return aid


def test_reconcile_to_integrating_stamps_open_run_completed(db):
    """Unit pin at the fix site: when reconcile_topic_state derives 'integrating'
    (all tasks verified, no merge proof yet), it stamps the bound thread's newest
    open run 'completed' — the same coupling f258871 gave mark_topic_integrating."""
    from dbops.db_topics import reconcile_topic_state  # re-export (avoids cycle)

    tid = db.create_thread("feat-topic", session_id="s")
    _seed_running_topic(db, tid, "T-recon")
    _bind_busy_agent(db, tid)

    state = reconcile_topic_state(db, "T-recon")

    assert state == "integrating"
    newest = db.get_runs(thread_id=tid, limit=1)[0]
    assert newest["status"] == "completed", (
        "run stuck 'dispatched' — reconcile→integrating did not stamp the run"
    )


def test_spool_graph_mark_task_drain_stamps_run_completed(db):
    """Ingress pin — the live drain path: a spooled graph_mark_task for the topic's
    last task, drained via the watchdog apply seam, must flip the newest run
    'completed' (via cmd_graph_mark_task → reconcile_topic_state → integrating)."""
    from juggle_spool_apply import apply_event
    from juggle_graph_reintegrate import _completion_recorded
    from dbops.db_topics import get_topic

    tid = db.create_thread("feat-topic", session_id="s")
    task = _seed_running_topic(db, tid, "T-drain", task_state="running")
    _bind_busy_agent(db, tid)

    ev = SpoolEvent(
        uuid="sa-mark-0001", type="graph_mark_task", agent_id="A", thread_id=tid,
        args=dict(task_id=task, fail=False, handoff="handed off"),
    )
    apply_event(db, ev)

    assert get_topic(db, "T-drain")["state"] == "integrating"
    newest = db.get_runs(thread_id=tid, limit=1)[0]
    assert newest["status"] == "completed", (
        "run stuck 'dispatched' after graph_mark_task drain — the spool-apply "
        "completion ingress reaches integrating via a seam that never stamps"
    )
    assert _completion_recorded(db, tid) is True
