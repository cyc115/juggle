"""juggle_graph_reconcile — auto-heal task nodes wedged in an in-flight state.

Owns: the per-tick reconcile pass that detects kind='task' nodes stuck in an
in-flight state ({dispatching, running, integrating}) whose dispatch thread is
NULL or resolves to a DEAD thread/agent (closed/archived thread, or
decommissioned/absent agent) and transitions them off the wedge. A node bound to
a LIVE (busy) agent is NEVER reset — the liveness signal is the same one
``juggle_watchdog.check_orphaned_threads`` uses (a busy agent assigned to the
thread), so a slow-but-alive agent survives.
Must not own: the state machine (dbops.db_node_machine), task CRUD / writes
(dbops.db_graph — every write here goes through ``task_transition``), or the
dispatcher claim loop (juggle_graph_dispatch).

Incident (2026-06-30): node R1 stuck 'dispatching' with a NULL dispatch thread
(residue of an earlier bind_thread crash). The watchdog skips tick-owned states
and the existing orphan recovery operates on THREADS, not on a task NODE wedged
in 'dispatching' — so it never self-healed and required a manual stale_reset.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from dbops import db_graph
from juggle_graph_status import IN_FLIGHT_STATES

_log = logging.getLogger("juggle-graph-reconcile")

# Guards the dispatch/bind window: a node just claimed (ready→dispatching) is
# legitimately NULL-threaded for the milliseconds until set_task_thread binds it,
# so only nodes older than this are reconcile candidates. Matches
# juggle_graph_dispatch.STALE_CLAIM_SECS for consistency with the flat sweep.
RECONCILE_STALE_SECS = 600

# Legal heal event per in-flight state. 'dispatching' reclaims to 'ready' (the
# R1 wedge — pre-execution, safe to re-dispatch). 'running'/'integrating' CANNOT
# stale_reset (illegal in the unified node machine — see db_node_machine), and a
# confirmed-dead agent there is a failed execution: the legal, reload-recoverable
# terminal. We deliberately do NOT re-dispatch them — an 'integrating' node may
# have ALREADY merged to main (integrate merges before mark_completion), so a
# silent re-run could double-merge/duplicate work; failing surfaces it safely.
_HEAL_EVENT = {
    "dispatching": "stale_reset",     # → ready
    "running": "exec_fail",           # → failed-exec
    "integrating": "integrate_fail",  # → failed-integration
}


def _live_thread_ids(db) -> set[str]:
    """Threads with a busy agent assigned — the watchdog's liveness signal
    (mirrors check_orphaned_threads). A slow-but-alive agent stays 'busy', so
    its node is protected from reconcile."""
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT assigned_thread FROM agents "
                "WHERE status='busy' AND assigned_thread IS NOT NULL"
            ).fetchall()
        return {r["assigned_thread"] for r in rows}
    except Exception:
        _log.exception("reconcile: live-thread scan failed — assuming none live")
        return set()


def reconcile_orphaned_inflight(
    db, *, stale_secs: int = RECONCILE_STALE_SECS
) -> list[str]:
    """Heal kind='task' nodes wedged in an in-flight state with a NULL/dead
    dispatch thread. Returns the healed task ids. Never raises (per-node guarded);
    a slow-but-alive (busy-agent-bound) node is never touched.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=stale_secs)
    ).isoformat()
    placeholders = ",".join("?" for _ in IN_FLIGHT_STATES)
    try:
        with db._connect() as conn:
            rows = conn.execute(
                f"SELECT id, state, "
                f"(SELECT depends_on_id FROM node_edges WHERE node_id=nodes.id "
                f"  AND kind='dispatch' LIMIT 1) AS thread_id "
                f"FROM nodes WHERE kind='task' AND state IN ({placeholders}) "
                f"AND updated_at < ?",
                (*IN_FLIGHT_STATES, cutoff),
            ).fetchall()
    except Exception:
        _log.exception("reconcile: orphan scan failed — skipping")
        return []

    live = _live_thread_ids(db)
    healed: list[str] = []
    for row in rows:
        task_id = row["id"]
        state = row["state"]
        thread_id = row["thread_id"]
        # SAFETY: a thread with a busy agent is LIVE — never reset working work.
        if thread_id is not None and thread_id in live:
            continue
        event = _HEAL_EVENT.get(state)
        if event is None:
            continue
        try:
            new_state = db_graph.task_transition(db, task_id, event)
        except Exception:
            _log.exception("reconcile: heal failed for task %s", task_id)
            continue
        healed.append(task_id)
        _log.warning(
            "reconcile: orphaned task %s in %r (thread=%s) → %s via %s",
            task_id, state, thread_id, new_state, event,
        )
    return healed
