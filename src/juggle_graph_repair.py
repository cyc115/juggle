"""Watchdog-tick repair sweeps (irl-backbone T1c + irl-repair T4).

Owns: sweep_orchestrator_ttl (orchestrator-kind notification TTL/dead-monitor
detection), reconcile_missing_topic_notifications (backfill for failed/wedged
topics that never got a TOPIC_STATUS row) — both fail-soft and idempotent,
called every watchdog tick — and (T4) sweep_repair_dispatch, the PRE-CLAIM
repair sweep graph_tick runs before its topic-claim loop: it hands a coder a
class playbook and re-dispatches it into the preserved worktree of every
failed-integration topic whose envelope is still repair-dispatchable.

Must not own: the monitor's own polling loop (juggle_monitor_daemon — only its
pidfile/cursor breadcrumbs are read) or the claim/hydrate machinery. The T4
sweep does not touch the state machine (a repaired topic stays in
failed-integration until its agent re-completes); it reuses graph_tick's own
internal ``dispatch`` callable so it goes through the tick's dispatch path, not
the CLI (which refuses tick-owned threads).
"""

from __future__ import annotations

import json
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


# ── irl-repair T4: pre-claim repair-dispatch sweep ──────────────────────────


def is_repair_dispatchable(envelope: dict | None) -> bool:
    """True iff a routine (non-machinery) failure is still under its retry caps.

    The signal is computed once, authoritatively, by
    ``juggle_integrate_envelope.record_refusal`` at fail time (it knows whether
    the retry policy allowed a fresh attempt) and stamped onto the envelope as
    ``repair_dispatchable``. machinery classes and cap/backstop breaches leave
    it False — so an exhausted or awaiting-orchestrator topic reserves nothing
    and never starves the claim loop (plan T4 DA #6)."""
    return bool((envelope or {}).get("repair_dispatchable"))


def _parse_envelope(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def sweep_repair_dispatch(db, project_id: str, *, dispatch, session_id: str = "") -> list[str]:
    """Dispatch a repair coder into the preserved worktree of each dispatchable
    failed-integration topic in ``project_id``. Returns the repaired topic ids.

    Ordered BEFORE graph_tick's claim loop so a repair — which unblocks
    dependents — outranks a fresh topic for the last agent slot (the conditional
    slot reservation: only a DISPATCHABLE repair takes a slot first; an
    exhausted topic is skipped and reserves nothing). ``dispatch`` is
    graph_tick's own internal dispatch callable (NOT the CLI), so the preserved
    worktree on the topic's still-bound thread is reused in place. Fail-soft: a
    capacity hit stops the sweep (retried next tick, slot still reserved); any
    other per-topic error is logged and skipped so one bad topic can't wedge it.
    """
    from dbops import db_topics
    from juggle_graph_hydration import build_repair_prompt

    dispatched: list[str] = []
    try:
        topics = db_topics.list_topics(db, project_id)
    except Exception:
        _log.exception("repair sweep: topic scan failed for project %s", project_id)
        return dispatched

    for topic in topics:
        if topic.get("state") != "failed-integration":
            continue
        thread_id = topic.get("thread_id")
        if not thread_id:
            continue  # thread unbound (reloaded) — worktree gone, nothing to repair
        envelope = _parse_envelope(topic.get("fail_envelope"))
        if not is_repair_dispatchable(envelope):
            continue

        try:
            dispatch(db, thread_id, build_repair_prompt(envelope), topic)
        except Exception as exc:  # noqa: BLE001 — classify below, never propagate
            from juggle_graph_dispatch import CapacityError
            if isinstance(exc, CapacityError):
                _log.info("repair sweep: capacity hit — topic %s deferred", topic["id"])
                break  # slot reserved: sweep re-runs first next tick
            _log.exception("repair sweep: dispatch failed for topic %s", topic["id"])
            continue

        # Consumed: flip dispatchable off so the next tick doesn't re-dispatch
        # while the agent works. Its re-integrate writes a fresh envelope
        # (success clears it; a re-failure recomputes dispatchability under the
        # now-incremented caps → repair_exhausted).
        envelope["repair_dispatchable"] = False
        envelope["repair_dispatched"] = True
        db_topics.set_topic_fail_envelope(db, topic["id"], json.dumps(envelope))
        db.emit_event(
            thread_id,
            f"⬢ repair dispatched for topic {topic['id']} "
            f"[{envelope.get('class')}] — {topic.get('title', '')}",
            session_id,
            kind=_ek.REPAIR_DISPATCHED,
            handled_by="watchdog",
        )
        dispatched.append(topic["id"])
    return dispatched


def run_repair_sweeps(db, project_ids, dispatch, session_id: str = "") -> list[str]:
    """graph_tick entry point: run sweep_repair_dispatch across every project
    (fail-soft — one bad project never wedges the tick) and return every
    repaired topic id. Kept here (not in graph_dispatch) so the dispatch module
    stays under the LOC gate (plan T4 module budget)."""
    repaired: list[str] = []
    for pid in project_ids:
        try:
            repaired += sweep_repair_dispatch(db, pid, dispatch=dispatch, session_id=session_id)
        except Exception:
            _log.exception("repair sweep: project %s failed — continuing", pid)
    return repaired


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
