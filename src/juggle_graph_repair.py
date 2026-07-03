"""Watchdog-tick repair sweeps (irl-backbone T1c).

Owns: sweep_orchestrator_ttl (orchestrator-kind notification TTL/dead-monitor
detection) and reconcile_missing_topic_notifications (backfill for
failed/wedged topics that never got a TOPIC_STATUS row). Both are fail-soft
and idempotent — safe to call every watchdog tick. Must not own dispatch
logic (juggle_graph_dispatch) or the monitor's own polling loop
(juggle_monitor_daemon) — only reads its pidfile/cursor breadcrumbs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from dbops import event_kinds as _ek

_log = logging.getLogger("juggle-graph-repair")

_TTL_THRESHOLD_SECS = 300  # 5 minutes
_DEAD_MONITOR_PREFIX = "[orc monitor dead]"
_TOPIC_FAILED_STATES = ("failed-exec", "failed-integration", "failed-verify", "blocked-failed")


def _live_monitor_cursors(juggle_dir=None) -> list[int]:
    """Persisted delivery cursor of every currently-live juggle-agent-monitor."""
    import juggle_monitor_daemon as _mon

    jdir = juggle_dir or _mon._JUGGLE_DIR
    cursors = []
    for pidfile in jdir.glob("monitor-*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            continue
        if not _mon._is_monitor_process(pid):
            continue
        session = pidfile.stem[len("monitor-"):]
        try:
            cursors.append(int(_mon._cursor_for(session).read_text().strip()))
        except (ValueError, OSError):
            cursors.append(0)
    return cursors


def sweep_orchestrator_ttl(db, *, now=None, threshold_secs=_TTL_THRESHOLD_SECS, juggle_dir=None):
    """Orchestrator-kind rows no live monitor has consumed, unconsumed past
    threshold_secs — or immediately eligible when NO monitor is live at all
    (nothing will ever deliver them). Files ONE deduped HIGH action item."""
    now = now or datetime.now(timezone.utc)
    live_cursors = _live_monitor_cursors(juggle_dir)
    monitor_alive = bool(live_cursors)
    max_cursor = max(live_cursors, default=0)

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at FROM notifications_v2 WHERE handled_by = 'orchestrator' "
            "AND id > ? ORDER BY id",
            (max_cursor,),
        ).fetchall()

    stale_ids = []
    for row in rows:
        if not monitor_alive:
            stale_ids.append(row["id"])
            continue
        try:
            created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - created).total_seconds() >= threshold_secs:
            stale_ids.append(row["id"])

    if not stale_ids:
        return []
    if any(a["message"].startswith(_DEAD_MONITOR_PREFIX) for a in db.get_open_action_items()):
        return []  # already flagged — dedup across ticks

    db.add_action_item(
        thread_id=None,
        message=(f"{_DEAD_MONITOR_PREFIX} {len(stale_ids)} orchestrator notification(s) "
                 f"unconsumed (ids {stale_ids[0]}-{stale_ids[-1]}). Re-run /juggle:start."),
        type_="failure", priority="high",
    )
    return stale_ids


def reconcile_missing_topic_notifications(db):
    """Backfill a TOPIC_STATUS notification for any failed/blocked-failed
    topic whose thread never got one (e.g. a crash between the state
    transition and emit_event)."""
    from dbops import db_topics

    placeholders = ",".join("?" for _ in _TOPIC_FAILED_STATES)
    with db._connect() as conn:
        topics = conn.execute(
            f"SELECT id, state FROM nodes WHERE kind='topic' AND state IN ({placeholders})",
            _TOPIC_FAILED_STATES,
        ).fetchall()

    backfilled = []
    for t in topics:
        topic = db_topics.get_topic(db, t["id"]) or {}
        thread_id = topic.get("thread_id")
        if not thread_id:
            continue
        with db._connect() as conn:
            has_notif = conn.execute(
                "SELECT 1 FROM notifications_v2 WHERE thread_id = ? AND kind = ? LIMIT 1",
                (thread_id, _ek.TOPIC_STATUS),
            ).fetchone()
        if has_notif:
            continue
        db.emit_event(
            thread_id=thread_id, session_id="", kind=_ek.TOPIC_STATUS,
            message=f"⬢ topic {t['id']} → {t['state']} (backfilled by reconciler)",
        )
        backfilled.append(t["id"])
    return backfilled


def run_tick_sweeps(db) -> None:
    """Both T1c sweeps, each independently fail-soft — call once per watchdog tick."""
    try:
        sweep_orchestrator_ttl(db)
    except Exception:
        _log.exception("graph repair: orchestrator TTL sweep failed")
    try:
        reconcile_missing_topic_notifications(db)
    except Exception:
        _log.exception("graph repair: notification reconcile failed")
