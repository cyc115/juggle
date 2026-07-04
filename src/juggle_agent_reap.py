"""juggle_agent_reap — binding-independent pool-agent + ledger reaping.

Fix 3 (2026-07-03 integrate-wedge RCA Q3): cmd_complete_agent reaped the pool
agent and closed the ledger run ONLY via ``get_agent_by_thread`` (status='busy'
AND assigned_thread=thread). In spool-replay-after-rebind that binding had
shifted, so the idle-write silently no-op'd and the run stayed 'dispatched' —
all 5 pool agents + 2 researchers ended CLEAN completions still 'busy'. This
module reaps the agent by the thread's OPEN ledger run (its recorded agent_id)
and closes the run by thread_id, independent of the live binding.

Must not own: the completion pipeline (juggle_cmd_agents_complete), graph/topic
state (dbops), or agent-pool CRUD — it only reads the ledger/pool and reaps.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _agent_from_open_run(db, thread_uuid):
    """The agent recorded on this thread's newest OPEN ('dispatched') ledger
    run — the binding-independent recovery path for reaping. Returned ONLY when
    it is safe to reap: the agent is idle/unassigned or still assigned to THIS
    thread, never an agent since re-dispatched and busy on a DIFFERENT thread."""
    try:
        runs = db.get_runs(thread_id=thread_uuid, limit=1)
    except Exception:
        return None
    if not runs or runs[0].get("status") != "dispatched":
        return None
    aid = runs[0].get("agent_id")
    if not aid:
        return None
    agent = db.get_agent(aid)
    if not agent:
        return None
    if agent.get("assigned_thread") in (thread_uuid, None):
        return agent
    return None


def reap_completed_agent(db, thread_uuid):
    """Reap the pool agent for a completed thread INDEPENDENT of a still-live
    thread binding, and return the reaped agent dict (or None).

    Prefer the live binding (``get_agent_by_thread``); else recover the agent
    from the thread's OPEN ledger run and reap it — never touching an agent busy
    on another thread. Also records the completion-duration metric.
    Best-effort; never raises."""
    try:
        agent = db.get_agent_by_thread(thread_uuid) or _agent_from_open_run(db, thread_uuid)
        if not agent:
            return None
        busy_since = agent.get("busy_since")
        if busy_since:
            try:
                busy_dt = datetime.fromisoformat(str(busy_since).replace("Z", "+00:00"))
                if busy_dt.tzinfo is None:
                    busy_dt = busy_dt.replace(tzinfo=timezone.utc)
                duration = (datetime.now(timezone.utc) - busy_dt).total_seconds()
                db.insert_agent_completion(role=agent.get("role"), duration_secs=duration)
            except (ValueError, TypeError):
                pass
        db.update_agent(agent["id"], status="idle", assigned_thread=None)
        return agent
    except Exception:
        return None


def close_thread_run_backstop(db, thread_uuid, output) -> None:
    """Fix 3 (ledger half): guarantee the thread's ledger run is closed by
    thread_id even when an early-return skipped the normal closer
    (mark_graph_topic ValueError / close_adhoc_run graph-skip). ``close_run``
    keys by thread_id and no-ops when no 'dispatched' run remains, so this is
    safe to call unconditionally as a backstop. Best-effort; never raises."""
    try:
        db.close_run(thread_uuid, output=output, diffstat=None, status="completed")
    except Exception:
        pass
