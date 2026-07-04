"""Spool replay of a mark/complete-kind event whose target ALREADY reached the
intended-or-further terminal state is an idempotent SUPERSEDED no-op — NOT a
dead-letter (2026-07-03 spool 0ecaef08 + 8960b2cc, action items 5226/5227;
same class as 5210/5218 for agent_complete).

When the watchdog integrates + reconciles a topic BEFORE draining the spool, a
still-pending graph_mark_task / agent_complete for a task that is already
'verified' used to hit mark_completion's terminal-state guard → sys.exit(1) →
dead-letter + HIGH action item. That is a false alarm: state is already correct
and the handoff is already recorded. These pins lock the superseded outcome, and
guard the two cases that MUST still dead-letter loudly: a contradicting event
(--fail against a verified task) and a genuine handler error (task missing).
"""
import os

import pytest

from dbops import db_graph as g
from dbops.spool import write_event, read_pending
from juggle_db import JuggleDB
from juggle_spool_apply import drain_spool


@pytest.fixture
def db():
    d = JuggleDB(os.environ["JUGGLE_DB_PATH"])
    d.init_db()
    return d


@pytest.fixture
def spool(tmp_path, monkeypatch):
    s = tmp_path / "spool"
    s.mkdir()
    monkeypatch.setattr("juggle_spool_apply.spool_dir", lambda: s)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return s


def _verified_task(db, task_id="a"):
    g.create_task(db, task_id=task_id, project_id="INBOX", title="A", prompt="do a")
    g.mark_completion(db, task_id, integrate_ok=True)  # open → … → verified
    assert g.get_task(db, task_id)["state"] == "verified"
    return task_id


def _journal_outcome(db, uuid):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT outcome FROM spool_journal WHERE uuid=?", (uuid,)
        ).fetchone()
    return row["outcome"] if row else None


def test_mark_task_replay_on_verified_task_is_superseded_not_dead_lettered(db, spool):
    task_id = _verified_task(db)
    uuid = write_event(spool, "graph_mark_task", "agent-1", "",
                       {"task_id": task_id, "fail": False, "handoff": "x landed"})

    stats = drain_spool(db)

    assert stats["dead"] == 0, "superseded replay must NOT dead-letter"
    assert _journal_outcome(db, uuid) == "superseded"
    # no dead file
    assert list((spool / "dead").glob("*.json")) == []
    # no HIGH failure action item
    assert not any(
        (i.get("priority") == "high") and "dead-letter" in (i.get("message") or "").lower()
        for i in db.get_open_action_items()
    )
    # the source file is consumed (unlinked), not left pending
    assert read_pending(spool) == []


def test_contradicting_fail_event_on_verified_task_still_dead_letters(db, spool):
    """PIN: a --fail event against an ALREADY-verified task CONTRADICTS the
    terminal state — that is a real inconsistency, must dead-letter loudly."""
    task_id = _verified_task(db)
    write_event(spool, "graph_mark_task", "agent-1", "",
                {"task_id": task_id, "fail": True, "handoff": None})

    stats = drain_spool(db)

    assert stats["dead"] == 1
    assert len(list((spool / "dead").glob("*.json"))) == 1


def test_genuine_missing_task_error_still_dead_letters(db, spool):
    """PIN: a mark event for a task that does not exist is never masqueraded as
    superseded — it eventually dead-letters loudly. Since a missing target is
    often TRANSIENT (T-fix-spool-drain-systemexit), the drain now RETRIES it
    in-order for a bounded number of ticks first; a genuinely-absent task still
    dead-letters once the retries exhaust."""
    from juggle_spool_apply import _RETRY_MAX_ATTEMPTS

    write_event(spool, "graph_mark_task", "agent-1", "",
                {"task_id": "no-such-task", "fail": False, "handoff": None})

    # First drains retry (not dead) while the task stays absent.
    first = drain_spool(db)
    assert first["dead"] == 0
    assert first["retried"] == 1
    assert len(list((spool / "dead").glob("*.json"))) == 0

    dead = 0
    for _ in range(_RETRY_MAX_ATTEMPTS + 1):
        dead = drain_spool(db)["dead"]
        if dead:
            break
    assert dead == 1
    assert len(list((spool / "dead").glob("*.json"))) == 1
