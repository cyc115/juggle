"""Tests for irl-backbone T1b: monitor generalization to all pushable kinds.

_poll_once must widen beyond 'done'-conversation-thread agent-completion rows
to ANY pushable kind (handled_by != 'watchdog'), while keeping the exact
back-compat `[LABEL] role: title` line format for agent-completion events.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops import event_kinds as ek
from juggle_db import JuggleDB
from juggle_monitor_daemon import _poll_once


def _db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _done_thread(d, title):
    tid = d.create_thread(title, session_id="s")
    d.set_thread_status(tid, "closed")  # → node state 'done'
    return tid


def test_agent_complete_keeps_back_compat_line_format(tmp_path):
    d = _db(tmp_path)
    tid = _done_thread(d, "fix the thing")
    d.emit_event(tid, "fix the thing: all done", "s", kind=ek.AGENT_COMPLETE)

    with d._connect() as conn:
        results, last_id = _poll_once(conn, 0)

    assert len(results) == 1
    _, line = results[0]
    label = d.get_thread(tid)["user_label"]
    assert line == f"[{label}] coder: fix the thing"
    assert last_id > 0


def test_watchdog_kind_is_never_emitted(tmp_path):
    d = _db(tmp_path)
    tid = _done_thread(d, "watchdog thread")
    d.emit_event(tid, "[Watchdog] auto-resolved prompt", "s", kind=ek.WATCHDOG_PROMPT)

    with d._connect() as conn:
        results, _ = _poll_once(conn, 0)

    assert results == []


def test_task_status_emitted_even_on_non_done_thread(tmp_path):
    """T1b widen: task/topic status events aren't tied to a 'done' conversation
    thread — the old query would have missed these entirely."""
    d = _db(tmp_path)
    tid = d.create_thread("open thread", session_id="s")  # stays 'open', not 'done'
    d.emit_event(tid, "⬢ graph task foo verified", "s", kind=ek.TASK_STATUS)

    with d._connect() as conn:
        results, _ = _poll_once(conn, 0)

    assert len(results) == 1
    _, line = results[0]
    assert line == "⬢ graph task foo verified"


def test_threadless_manual_notification_is_emitted(tmp_path):
    d = _db(tmp_path)
    d.emit_event(None, "⬢ topic ready: T2 — do the thing", "s", kind=ek.TOPIC_STATUS)

    with d._connect() as conn:
        results, _ = _poll_once(conn, 0)

    assert len(results) == 1
    assert results[0][1] == "⬢ topic ready: T2 — do the thing"


def test_more_than_three_same_kind_coalesce_to_one_line(tmp_path):
    d = _db(tmp_path)
    for i in range(5):
        d.emit_event(None, f"⬢ autopilot dispatched task t{i}", "s", kind=ek.AUTOPILOT_DISPATCH)
    # AUTOPILOT_DISPATCH is handled_by=watchdog — use a pushable kind instead.
    with d._connect() as conn:
        conn.execute("DELETE FROM notifications_v2")
        conn.commit()
    for i in range(5):
        d.emit_event(None, f"⬢ topic ready: T{i}", "s", kind=ek.TOPIC_STATUS)

    with d._connect() as conn:
        results, last_id = _poll_once(conn, 0)

    assert len(results) == 1
    _, line = results[0]
    assert "5" in line
    assert ek.TOPIC_STATUS in line
    assert last_id > 0


def test_three_or_fewer_same_kind_are_not_coalesced(tmp_path):
    d = _db(tmp_path)
    for i in range(3):
        d.emit_event(None, f"⬢ topic ready: T{i}", "s", kind=ek.TOPIC_STATUS)

    with d._connect() as conn:
        results, _ = _poll_once(conn, 0)

    assert len(results) == 3
