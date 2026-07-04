"""Incident regression (T-fix-spool-drain-systemexit, 2026-07-04): the watchdog
spool drain dead-lettered VALID events with an opaque "SystemExit … code=1"
whenever the event's target thread/task was TRANSIENTLY absent at first-drain
time — created moments earlier and present by a later drain (every such event
applied cleanly on manual `juggle spool replay`). RCA: a not-found precondition
is side-effect-free (it is the handler's FIRST check, before any mutation) and
resolvable on a later tick, but the drain treated it as permanent poison and the
dead_reason ("code=1") hid the underlying "… not found" message.

These pins lock the fix:
  1. a missing-target event RETRIES in-order for a bounded number of drains
     instead of dead-lettering on the first;
  2. it applies once the target appears;
  3. after the bounded attempts it dead-letters with the handler's REAL message
     captured into dead_reason (never the opaque "code=1").
"""
import json
import os

import pytest

from dbops import db_graph as g
from dbops.spool import read_pending, write_event
from juggle_db import JuggleDB
from juggle_spool_apply import _RETRY_MAX_ATTEMPTS, drain_spool


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


def _dead_files(spool):
    return list((spool / "dead").glob("*.json"))


def _drain_until_dead(db, spool):
    """Drain repeatedly (bounded) until the event dead-letters; return dead count."""
    for _ in range(_RETRY_MAX_ATTEMPTS + 2):
        dead = drain_spool(db)["dead"]
        if dead:
            return dead
    return 0


def test_missing_task_retries_in_order_not_dead_lettered_on_first_drain(db, spool):
    write_event(spool, "graph_mark_task", "agent-1", "",
                {"task_id": "later-task", "fail": False, "handoff": "h"})
    stats = drain_spool(db)
    assert stats["dead"] == 0, "a transiently-missing target must not dead-letter on first drain"
    assert stats["retried"] == 1
    # left pending for the next tick, NOT moved to dead
    assert _dead_files(spool) == []
    pend = read_pending(spool)
    assert len(pend) == 1 and pend[0].attempts == 1
    # no HIGH dead-letter action item filed yet
    assert not any("dead-letter" in (i.get("message") or "").lower()
                   for i in db.get_open_action_items())


def test_missing_task_applies_once_target_appears_before_exhaustion(db, spool):
    write_event(spool, "graph_mark_task", "agent-1", "",
                {"task_id": "appears", "fail": False, "handoff": "h"})
    assert drain_spool(db)["retried"] == 1  # target absent → retry, not dead
    g.create_task(db, task_id="appears", project_id="INBOX", title="A", prompt="do")
    stats = drain_spool(db)
    assert stats["applied"] == 1
    assert stats["dead"] == 0
    assert g.get_task(db, "appears")["state"] == "verified"
    assert read_pending(spool) == []


def test_missing_task_dead_letters_after_bounded_attempts_with_real_reason(db, spool):
    write_event(spool, "graph_mark_task", "agent-1", "",
                {"task_id": "never", "fail": False, "handoff": "h"})
    assert _drain_until_dead(db, spool) == 1, "must dead-letter after exhausting bounded retries"
    files = _dead_files(spool)
    assert len(files) == 1
    reason = json.loads(files[0].read_text()).get("dead_reason", "")
    assert "not found" in reason.lower(), f"opaque reason not captured: {reason!r}"
    assert reason.strip() != "SystemExit during replay of graph_mark_task: code=1"


def test_missing_thread_agent_complete_retries_then_dead_with_real_reason(db, spool):
    # 36-char UUID that does not exist → _resolve_thread prints "no thread with id …"
    ghost = "00000000-0000-0000-0000-000000000000"
    write_event(spool, "agent_complete", "agent-1", ghost,
                {"thread_id": ghost, "result_summary": "x", "retain_text": None,
                 "open_questions": None, "handoff": None, "role": None})
    first = drain_spool(db)
    assert first["dead"] == 0 and first["retried"] == 1
    assert _drain_until_dead(db, spool) == 1
    reason = json.loads(_dead_files(spool)[0].read_text())["dead_reason"].lower()
    assert "no thread" in reason or "not found" in reason
