"""Tests for irl-backbone T1c: orchestrator TTL sweep + notification reconciler.

Three pins (plan DA log):
1. No live monitor -> immediately eligible, no 5-min wait.
2. Dedup: a second sweep while the action item is still open files nothing new.
3. Reconciler never double-backfills a topic that already has a TOPIC_STATUS row.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops import event_kinds as ek
from juggle_db import JuggleDB
from juggle_graph_repair import (
    reconcile_missing_topic_notifications,
    sweep_orchestrator_ttl,
)


def _db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


# --- Pin 1: no live monitor -> immediately eligible ------------------------

def test_no_live_monitor_flags_immediately(tmp_path):
    d = _db(tmp_path)
    d.emit_event(None, "⬢ topic ready: T1", "s", kind=ek.TOPIC_STATUS)  # orchestrator kind

    stale = sweep_orchestrator_ttl(d, juggle_dir=tmp_path / "no-monitor-here")

    assert len(stale) == 1
    items = d.get_open_action_items()
    assert any(a["message"].startswith("[orc monitor dead]") for a in items)


def test_recent_row_with_live_monitor_is_not_flagged(tmp_path, monkeypatch):
    import juggle_graph_repair as repair

    monkeypatch.setattr(repair, "_live_monitor_cursors", lambda juggle_dir=None: [999])
    d = _db(tmp_path)
    d.emit_event(None, "⬢ topic ready: T1", "s", kind=ek.TOPIC_STATUS)

    stale = sweep_orchestrator_ttl(d)

    assert stale == []
    assert d.get_open_action_items() == []


# --- Pin 2: dedup across sweeps ---------------------------------------------

def test_sweep_dedupes_across_repeated_calls(tmp_path):
    d = _db(tmp_path)
    d.emit_event(None, "⬢ topic ready: T1", "s", kind=ek.TOPIC_STATUS)

    sweep_orchestrator_ttl(d, juggle_dir=tmp_path / "no-monitor")
    sweep_orchestrator_ttl(d, juggle_dir=tmp_path / "no-monitor")

    items = [a for a in d.get_open_action_items() if a["message"].startswith("[orc monitor dead]")]
    assert len(items) == 1


def test_watchdog_kind_rows_are_never_swept(tmp_path):
    d = _db(tmp_path)
    d.emit_event(None, "[Watchdog] auto-resolved prompt", "s", kind=ek.WATCHDOG_PROMPT)

    stale = sweep_orchestrator_ttl(d, juggle_dir=tmp_path / "no-monitor")

    assert stale == []


# --- Pin 3: reconciler never double-backfills -------------------------------

def _make_failed_topic(d, project_id, state):
    with d._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "created_at, updated_at) VALUES (?, 'topic', 'T', '', ?, ?, ?, ?)",
            ("topic-1", state, project_id, "2026-01-01 00:00", "2026-01-01 00:00"),
        )
        conn.commit()
    tid = d.create_thread("topic thread", session_id="s", project_id=project_id)
    with d._connect() as conn:
        conn.execute(
            "INSERT INTO node_edges (node_id, depends_on_id, kind) VALUES (?, ?, 'dispatch')",
            ("topic-1", tid),
        )
        conn.commit()
    return tid


def test_reconciler_backfills_missing_notification(tmp_path):
    d = _db(tmp_path)
    pid = d.create_project("P", "")
    thread_id = _make_failed_topic(d, pid, "failed-exec")

    backfilled = reconcile_missing_topic_notifications(d)

    assert backfilled == ["topic-1"]
    with d._connect() as conn:
        row = conn.execute(
            "SELECT kind FROM notifications_v2 WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    assert row["kind"] == ek.TOPIC_STATUS


def test_reconciler_skips_topic_that_already_has_a_notification(tmp_path):
    d = _db(tmp_path)
    pid = d.create_project("P", "")
    thread_id = _make_failed_topic(d, pid, "failed-exec")
    d.emit_event(thread_id, "⬢ topic topic-1 → failed-exec", "s", kind=ek.TOPIC_STATUS)

    backfilled = reconcile_missing_topic_notifications(d)

    assert backfilled == []
    with d._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications_v2 WHERE thread_id = ?", (thread_id,)
        ).fetchone()["c"]
    assert n == 1
