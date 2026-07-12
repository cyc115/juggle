"""juggle_watchdog_orphans — orphaned background-thread detection.

Owns: check_orphaned_threads (Loop 2 orphan scan + auto-recovery).
Must not own: single-agent inspection (inspect_agent), batch recovery
(execute_recovery), classifier constants (see juggle_watchdog.py).

Re-exported by juggle_watchdog.py so existing imports
``from juggle_watchdog import check_orphaned_threads`` continue to work.
"""

from __future__ import annotations

import logging
from typing import Any

from dbops import event_kinds as _ek

_log = logging.getLogger(__name__)

_ORPHAN_MAX_RECOVERY_ATTEMPTS = 2


def check_orphaned_threads(
    db: Any,
    *,
    orphan_threshold: float = 300.0,
    dedup_window_hours: float = 24.0,
    mgr: Any = None,
    max_recovery_attempts: int = _ORPHAN_MAX_RECOVERY_ATTEMPTS,
) -> list[str]:
    """Scan background threads with no active agent; auto-recover or file action items.

    Returns list of orphaned thread_ids detected this cycle. Uses 24h dedup guard.
    When mgr is provided and last_dispatched_task exists, auto-recovers by re-dispatching
    the last task to a fresh agent (reusing execute_recovery spawn path).
    Falls back to manual action item if: no mgr, no task, pool full, or max attempts reached.
    """
    # Import get_session_id from juggle_watchdog to avoid duplication.
    # juggle_watchdog imports this module at top level, so this must stay
    # deferred to resolve the circular reference.
    import juggle_watchdog as _wdog
    from datetime import datetime, timezone, timedelta

    from dbops.terminal_states import topic_work_landed

    now = datetime.now(timezone.utc)
    dedup_cutoff = (now - timedelta(hours=dedup_window_hours)).isoformat()

    with db._connect() as conn:
        thread_rows = conn.execute(  # P8 Task 3.1 (R2-1): read background from nodes
            "SELECT * FROM nodes WHERE kind='conversation' AND state='background'"
        ).fetchall()
        threads = [dict(r) for r in thread_rows]
        busy_rows = conn.execute(
            "SELECT assigned_thread FROM agents WHERE status='busy' AND assigned_thread IS NOT NULL"
        ).fetchall()
        busy_thread_ids = {r["assigned_thread"] for r in busy_rows}
        busy_count = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE status='busy'"
        ).fetchone()[0]

    orphaned: list[str] = []

    for thread in threads:
        thread_id = thread["id"]
        if thread_id in busy_thread_ids:
            continue

        last_active_at = thread.get("last_active_at")
        if not last_active_at:
            continue

        try:
            last_dt = datetime.fromisoformat(last_active_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            orphaned_for = (now - last_dt).total_seconds()
        except (ValueError, TypeError):
            continue

        if orphaned_for < orphan_threshold:
            continue

        # Landed-ad-hoc guard (2026-07-07 #5558/#5564): skip action item +
        # auto-recovery for work already merged — never a real orphan.
        if topic_work_landed(thread):
            continue

        with db._connect() as conn:
            recent = conn.execute(
                "SELECT id FROM watchdog_events "
                "WHERE thread_id=? AND event_type='orphaned' AND created_at > ?",
                (thread_id, dedup_cutoff),
            ).fetchone()
        if recent:
            continue

        label = thread.get("user_label") or thread.get("label") or thread_id[:8]
        mins = int(orphaned_for // 60)
        last_task = thread.get("last_dispatched_task")
        role = thread.get("last_dispatched_role")  # None = unknown; auto-recovery skipped
        # Fix 4 (mirrors execute_recovery): never forward the stale snapshot
        # model — always let spawn_agent re-resolve from current config
        # (2026-07-01 coder model config ignored).
        model = None

        # Attempt auto-recovery when possible
        did_recover = False
        if mgr is not None and last_task:
            with db._connect() as conn:
                attempt_count = conn.execute(
                    "SELECT COUNT(*) FROM watchdog_events "
                    "WHERE thread_id=? AND event_type='orphan_recovery'",
                    (thread_id,),
                ).fetchone()[0]

            try:
                from juggle_settings import resolve_max_agents
                max_agents = resolve_max_agents()
            except Exception:
                max_agents = 20

            pool_full = busy_count >= max_agents

            if attempt_count < max_recovery_attempts and not pool_full and role:
                try:
                    new_agent = mgr.spawn_agent(db, role=role, model=model)
                    new_agent_id = new_agent["id"]
                    new_pane_id = new_agent["pane_id"]
                    ts = now.isoformat()
                    db.update_agent(
                        new_agent_id,
                        status="busy",
                        assigned_thread=thread_id,
                        last_active=ts,
                        busy_since=ts,
                        last_task=last_task,
                    )
                    db.set_conversation_background(thread_id)
                    mgr.send_task(new_pane_id, last_task)
                    db.add_watchdog_event(
                        agent_id="orphan_detector",
                        thread_id=thread_id,
                        event_type="orphan_recovery",
                        snapshot_path=None,
                    )
                    _sid = ""
                    try:
                        _sid = _wdog.get_session_id(db)
                    except Exception:
                        pass
                    db.emit_event(
                        thread_id=thread_id, session_id=_sid, kind=_ek.WATCHDOG_RECOVERY,
                        message=(
                            f"[Watchdog] [{label}] orphaned thread auto-recovery: "
                            f"re-dispatched to agent {new_agent_id[:8]} "
                            f"(attempt {attempt_count + 1}/{max_recovery_attempts}, "
                            f"{mins} min no agent)"
                        ),
                    )
                    did_recover = True
                    _log.info(
                        "Watchdog: orphan auto-recovery — thread %s re-dispatched to agent %s (attempt %d)",
                        thread_id[:8],
                        new_agent_id[:8],
                        attempt_count + 1,
                    )
                except Exception as exc:
                    _log.error(
                        "Watchdog: orphan auto-recovery failed for thread %s: %s",
                        thread_id[:8],
                        exc,
                    )

        if not did_recover:
            task_snippet = f" Last task: {last_task[:80]}..." if last_task else ""
            db.add_action_item(
                thread_id=thread_id,
                message=(
                    f"[RQ] [{label}] orphaned thread — auto-recovery exhausted. "
                    f"Decide: re-dispatch / abandon / investigate. "
                    f"Cause: background thread with no agent for {mins} min.{task_snippet}"
                ),
                type_="failure",
                priority="high",
            )

        # DA-7: use sentinel agent_id, not empty string
        db.add_watchdog_event(
            agent_id="orphan_detector",
            thread_id=thread_id,
            event_type="orphaned",
            snapshot_path=None,
        )
        orphaned.append(thread_id)
        _log.warning(
            "Watchdog: orphaned thread %s (%s, %d min no agent)",
            thread_id[:8],
            label,
            mins,
        )

    return orphaned
