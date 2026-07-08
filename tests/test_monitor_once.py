"""Tests for the --once one-shot poll mode (legacy-monitor-cron plan,
2026-07-07): a cron fallback for the Monitor tool on machines with telemetry
disabled.

``run_once`` must reuse the EXACT event surface (_poll_once) and per-session
cursor (_load_cursor/_save_cursor) as the streaming daemon so a --once poll
and the streaming daemon never double-emit the same event.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops import event_kinds as ek
from juggle_db import JuggleDB
from juggle_monitor_daemon import _load_cursor, _poll_once, run_once


def _db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _done_thread(d, title):
    tid = d.create_thread(title, session_id="s")
    d.set_thread_status(tid, "closed")
    return tid


def test_once_prints_new_events_then_advances_cursor(tmp_path, capsys):
    """The literal plan acceptance scenario: seed events against a DB that has
    NEVER had a monitor cursor (no cursor file at all yet) — the first --once
    ever run must still surface them. (Unlike the streaming daemon's own
    first-run, which intentionally baselines at MAX(id) to avoid replaying
    history to a live consumer, --once IS the delivery mechanism for whatever
    piled up before its first poll — skipping it would silently drop real
    completions.)"""
    d = _db(tmp_path)
    tid = _done_thread(d, "fix the thing")
    d.emit_event(tid, "fix the thing: all done", "s", kind=ek.AGENT_COMPLETE)

    cursor_path = tmp_path / "monitor-once.cursor"
    assert not cursor_path.exists()
    run_once(db_path=Path(d.db_path), cursor_path=cursor_path)

    out = capsys.readouterr().out.strip()
    label = d.get_thread(tid)["user_label"]
    assert out == f"[{label}] coder: fix the thing"
    assert cursor_path.exists()


def test_once_is_silent_when_nothing_new(tmp_path, capsys):
    d = _db(tmp_path)
    cursor_path = tmp_path / "monitor-once.cursor"

    run_once(db_path=Path(d.db_path), cursor_path=cursor_path)

    assert capsys.readouterr().out == ""


def test_once_never_reemits_on_rerun(tmp_path, capsys):
    d = _db(tmp_path)
    cursor_path = tmp_path / "monitor-once.cursor"
    tid = _done_thread(d, "fix the thing")
    d.emit_event(tid, "fix the thing: all done", "s", kind=ek.AGENT_COMPLETE)

    run_once(db_path=Path(d.db_path), cursor_path=cursor_path)
    capsys.readouterr()  # discard first run's output

    run_once(db_path=Path(d.db_path), cursor_path=cursor_path)

    assert capsys.readouterr().out == ""


def test_once_shares_cursor_with_streaming_daemon(tmp_path, capsys):
    """A --once poll and the streaming daemon's own _poll_once, pointed at the
    SAME (session-shared) cursor file, must never double-emit one event."""
    d = _db(tmp_path)
    cursor_path = tmp_path / "monitor-shared.cursor"
    tid = _done_thread(d, "fix the thing")
    d.emit_event(tid, "fix the thing: all done", "s", kind=ek.AGENT_COMPLETE)

    db_path = Path(d.db_path)

    run_once(db_path=db_path, cursor_path=cursor_path)
    assert capsys.readouterr().out.strip() != ""

    last_seen_id = _load_cursor(cursor_path, db_path)
    with d._connect() as conn:
        results, _ = _poll_once(conn, last_seen_id)
    assert results == []
