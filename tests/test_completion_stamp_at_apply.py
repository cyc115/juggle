"""Regression pin — the agent_runs 'completed' stamp MUST land in the dbops
spool-apply of agent_complete (the topic→'integrating' transition), NOT only in
the cmd-layer backstop that runs much later (fix-completion-stamp-at-apply).

INCIDENT (2026-07-04 05:00-06:10, 7 occurrences RU RV RW RX RY RZ SA): every
completed coder left its thread's NEWEST agent_runs row at 'dispatched'. The
reintegrate sweep's bound-agent guard (_bound_agent_blocks → _completion_recorded)
therefore refused to spawn the integrate gate for each integrating topic until an
operator ran `agent release --force` — sitting the full REINTEGRATE_STALE_BOUND_SECS
(30 min) per topic while the pool starved.

ROOT CAUSE: the ONLY complete-path stamp was close_thread_run_backstop at the TAIL
of cmd_complete_agent, which runs long AFTER start_detached_integrate has already
transitioned the topic to 'integrating' (dbops mark_topic_integrating). When the
detached gate spawn failed/raised — or the watchdog process (which advances HEAD on
every integrate and self-restarts) died in that window — cmd_complete_agent aborted
after the topic transition but before the tail stamp. The topic sat 'integrating',
the run stuck 'dispatched', the agent stuck busy-bound, and the guard blocked.

FIX: stamp the thread's newest run 'completed' INSIDE mark_topic_integrating, the
same dbops marking that walks the topic to 'integrating' — independent of the
detached integrate subprocess and of every downstream cmd-layer step. The tail
backstop stays.

This pin reproduces the incident by making the detached spawn raise AFTER the topic
transition: RED on the old code (run stuck 'dispatched', guard blocks), GREEN once
the stamp moves into mark_topic_integrating.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(juggle_cmd_agents_common, "get_db", lambda: d)
    # Watchdog/spool-apply context: cmd_* run the write directly (never re-spool).
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return d


def _seed_running_topic(db, thread_id, topic_id):
    """An integrate-worthy topic (all member tasks 'verified') bound to a worktree
    thread and in 'running' — the exact complete-time shape the detached path hits."""
    from dbops import db_graph, db_topics

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    task = f"{topic_id}-t0"
    db_graph.create_task(db, task_id=task, project_id="INBOX", title=task, prompt="x")
    db_graph.set_task_topic(db, task, topic_id)
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='running' WHERE id=? AND kind='topic'",
                  (topic_id,))
        c.execute("UPDATE nodes SET state='verified' WHERE id=?", (task,))
        c.commit()
    db_topics.set_topic_thread(db, topic_id, thread_id)


def _bind_busy_agent(db, thread_id):
    """Bind a busy pool agent to the thread + open its dispatched ledger run —
    the pre-completion state the watchdog leaves before draining the spool."""
    aid = db.create_agent(role="coder", pane_id="p1", repo_path="/repo")
    db.update_agent(aid, status="busy", assigned_thread=thread_id,
                    busy_since=datetime.now(timezone.utc).isoformat())
    db.insert_agent_run(
        thread_id=thread_id, input_prompt="do the thing", agent_id=aid, role="coder",
        model="claude", harness="claude", project_id=None, topic_id=None, task_id=None,
    )
    return aid


def test_completed_stamp_survives_detached_gate_failure_after_transition(db):
    """The incident, reproduced: the topic reaches 'integrating' but the detached
    integrate spawn dies before the cmd-layer tail stamp. The run MUST still be
    'completed' (stamped at the dbops transition) so the reintegrate bound-agent
    guard does NOT block — even though the agent is still busy-bound (never reaped,
    because completion aborted after the transition)."""
    from juggle_graph_reintegrate import _bound_agent_blocks, _completion_recorded
    from juggle_spool_apply import apply_event
    from dbops.db_topics import get_topic

    tid = db.create_thread("feat-topic", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_BM",
                     main_repo_path="/repo")
    _seed_running_topic(db, tid, "T-stamp")
    _bind_busy_agent(db, tid)

    ev = SpoolEvent(
        uuid="sa-complete-0001", type="agent_complete", agent_id="A", thread_id=tid,
        args=dict(thread_id=tid, result_summary="done", handoff="handed off",
                  role="coder"),
    )

    # The detached gate dies right after the topic transition — the incident's
    # observed "complete-time detached gates keep failing to appear" window. The
    # cmd-layer tail stamp is thus never reached this apply.
    def _boom(*a, **k):
        raise RuntimeError("detached integrate spawn died")

    with patch("juggle_integrate_spawn.spawn_detached_integrate", side_effect=_boom):
        apply_event(db, ev)  # may report failure; the ledger stamp is what matters

    # The topic transitioned to 'integrating' — the pre-condition for the wedge.
    assert get_topic(db, "T-stamp")["state"] == "integrating"

    # The run is stamped 'completed' at the transition, NOT left 'dispatched'.
    newest = db.get_runs(thread_id=tid, limit=1)[0]
    assert newest["status"] == "completed", (
        "run stuck 'dispatched' — the stamp is still downstream of the transition"
    )
    assert _completion_recorded(db, tid) is True

    # The agent is still busy-bound (completion aborted before the reap), the exact
    # busy-bound state the incident left — yet the guard now clears via the stamp.
    assert db.get_agent_by_thread(tid) is not None
    topic = get_topic(db, "T-stamp")
    assert _bound_agent_blocks(db, topic, tid, datetime.now(timezone.utc)) is False


def test_mark_topic_integrating_stamps_open_run_completed(db):
    """Unit pin at the fix site: mark_topic_integrating stamps the bound thread's
    newest open run 'completed', in the same dbops marking that transitions the
    topic — independent of any completion handler."""
    from dbops.db_topics_marking import mark_topic_integrating

    tid = db.create_thread("feat-topic", session_id="s")
    _seed_running_topic(db, tid, "T-unit")
    _bind_busy_agent(db, tid)

    state = mark_topic_integrating(db, "T-unit", handoff="handed off")

    assert state == "integrating"
    newest = db.get_runs(thread_id=tid, limit=1)[0]
    assert newest["status"] == "completed"
    assert newest["output"] == "handed off"
