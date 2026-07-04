"""Regression pins — spool-applied agent_complete / agent_fail MUST stamp the
thread's newest agent_runs row (fix-agent-runs-completion-status).

INCIDENT (2026-07-04 05:00-05:30, live on 5/5 agents RU RV RW RX RY): every
agent_complete applied by the spool flipped its topic to 'integrating' and closed
the thread, but the thread's newest agent_runs row stayed status='dispatched'.
Consequence: _completion_recorded() (juggle_graph_reintegrate) returned False, so
the reintegrate bound-agent guard blocked the re-integrate sweep for up to
REINTEGRATE_STALE_BOUND_SECS per topic — agents sat busy-bound, the pool starved,
and the operator had to `agent release --force` all five to unwedge the merge queue.

The agent_complete leg was resolved on this branch by the detached-integrate
handoff (complete-agent returns fast and its ledger backstop stamps the run
'completed' — the inline gate no longer dies inside the watchdog before the stamp).
These pins LOCK that behavior. The agent_fail leg was still open: cmd_fail_agent
never touched agent_runs, so a persistently-failed agent's newest run stayed
'dispatched' forever — this file drives that fix (run → 'failed').
"""
from __future__ import annotations

import subprocess as real_subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops.spool import SpoolEvent
from juggle_db import JuggleDB


class _AliveProc:
    """A detached integrate still running its gate — proves complete-agent
    returned WITHOUT waiting for the merge (the RC1 detachment)."""

    def poll(self):
        return None


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    import juggle_cmd_agents_common

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(juggle_cmd_agents_common, "get_db", lambda: d)
    # Watchdog/spool-apply context: cmd_* run the write directly (never re-spool).
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return d


def _seed_running_topic(db, thread_id, topic_id, *, all_verified):
    """A topic bound to a worktree thread, in 'running'. all_verified=True is the
    integrate-worthy complete shape (detached integrate); False is the agent-death
    shape a persistent failure hits."""
    from dbops import db_graph, db_topics

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    task = f"{topic_id}-t0"
    db_graph.create_task(db, task_id=task, project_id="INBOX", title=task, prompt="x")
    db_graph.set_task_topic(db, task, topic_id)
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='running' WHERE id=? AND kind='topic'",
                  (topic_id,))
        if all_verified:
            c.execute("UPDATE nodes SET state='verified' WHERE id=?", (task,))
        c.commit()
    db_topics.set_topic_thread(db, topic_id, thread_id)


def _bind_busy_agent(db, thread_id):
    """Bind a busy pool agent to the thread + open its dispatched ledger run —
    the exact pre-completion state the watchdog leaves before draining the spool."""
    aid = db.create_agent(role="coder", pane_id="p1", repo_path="/repo")
    db.update_agent(aid, status="busy", assigned_thread=thread_id,
                    busy_since=datetime.now(timezone.utc).isoformat())
    db.insert_agent_run(
        thread_id=thread_id, input_prompt="do the thing", agent_id=aid, role="coder",
        model="claude", harness="claude", project_id=None, topic_id=None, task_id=None,
    )
    return aid


def test_spool_agent_complete_stamps_run_completed(db):
    """agent_complete applied by the spool → the thread's newest run flips to
    'completed', _completion_recorded() is True, and the reintegrate bound-agent
    guard does NOT block (the incident's exact failure inverted)."""
    from juggle_graph_reintegrate import _bound_agent_blocks, _completion_recorded
    from juggle_spool_apply import apply_event
    from dbops.db_topics import get_topic

    tid = db.create_thread("feat-topic", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_BM",
                     main_repo_path="/repo")
    _seed_running_topic(db, tid, "T-complete", all_verified=True)
    _bind_busy_agent(db, tid)

    ev = SpoolEvent(
        uuid="rz-complete-0001", type="agent_complete", agent_id="A", thread_id=tid,
        args=dict(thread_id=tid, result_summary="done", handoff="handed off",
                  role="coder"),
    )
    with patch.object(real_subprocess, "Popen",
                      side_effect=lambda *a, **k: _AliveProc()):
        ok, msg = apply_event(db, ev)
    assert ok, msg

    newest = db.get_runs(thread_id=tid, limit=1)[0]
    assert newest["status"] == "completed"
    assert _completion_recorded(db, tid) is True

    topic = get_topic(db, "T-complete")
    assert _bound_agent_blocks(db, topic, tid, datetime.now(timezone.utc)) is False


def test_spool_agent_fail_stamps_run_failed(db):
    """agent_fail (persistent, no recovery) applied by the spool → the thread's
    newest run flips to 'failed', not left 'dispatched'. Before the fix
    cmd_fail_agent never touched agent_runs, so the ledger row stuck 'dispatched'
    and `juggle runs` misreported a dead run as still in flight."""
    from juggle_spool_apply import apply_event

    tid = db.create_thread("feat-topic", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_BM",
                     main_repo_path="/repo")
    _seed_running_topic(db, tid, "T-fail", all_verified=False)
    _bind_busy_agent(db, tid)

    ev = SpoolEvent(
        uuid="rz-fail-0001", type="agent_fail", agent_id="A", thread_id=tid,
        args=dict(thread_id=tid, error="unrecoverable boom",
                  failure_type="persistent", max_retries=0,
                  recovery_dispatched=False),
    )
    ok, msg = apply_event(db, ev)
    assert ok, msg

    newest = db.get_runs(thread_id=tid, limit=1)[0]
    assert newest["status"] == "failed"


def test_spool_agent_fail_transient_leaves_run_dispatched(db):
    """A transient failure keeps the thread 'running' for retry — the still-open
    run MUST stay 'dispatched' (a retry re-dispatch supersedes it), never wrongly
    marked terminal."""
    from juggle_spool_apply import apply_event

    tid = db.create_thread("feat-topic", session_id="s")
    _seed_running_topic(db, tid, "T-transient", all_verified=False)
    _bind_busy_agent(db, tid)

    ev = SpoolEvent(
        uuid="rz-fail-transient", type="agent_fail", agent_id="A", thread_id=tid,
        args=dict(thread_id=tid, error="rate limited",
                  failure_type="transient", max_retries=1,
                  recovery_dispatched=False),
    )
    ok, msg = apply_event(db, ev)
    assert ok, msg

    newest = db.get_runs(thread_id=tid, limit=1)[0]
    assert newest["status"] == "dispatched"
