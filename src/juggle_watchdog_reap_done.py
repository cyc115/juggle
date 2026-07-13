"""juggle_watchdog_reap_done — reaper for COMPLETED-but-unreleased pool agents.

2026-07-07 completed-agents-leak incident: five coder agents whose work had
COMPLETED sat status=busy for 15-35h, idle at their tmux prompt, never released —
saturating the pool (5/5) so every new ``agent get`` failed "pool full". The
existing stalled/crashed reaper (juggle_watchdog.execute_recovery) only targets
STALLED/CRASHED agents; the stall detector (juggle_watchdog_stall) only nudges.
Neither returns a *finished* agent (bound topic terminal, agent idle) to the pool.

This reaper closes that gap as ROUTINE cleanup: for each busy interactive agent
that is (i) idle at its pane (NOT mid-turn) AND (ii) bound to a topic whose work
has already landed (``dbops.terminal_states.topic_work_landed`` — the single
signal shared with the re-dispatch guard + release-reconcile), it returns the
agent to the pool (status='idle') and closes its ledger run. It is DISTINCT from
the stalled/crashed reaper and never fires on an agent legitimately awaiting its
next task in an active dispatch (its topic is not terminal → skipped) or one that
is mid-turn (a submission marker is visible → skipped).

Pane retirement is deliberately left to the existing idle-TTL reaper
(juggle_tmux.reap_stale_agents): killing an idle agent's pane here would break
pane reuse. Self-contained + fully injectable (capture / markers / active-pattern)
to mirror juggle_watchdog_stall's testable driver shape.

The Fault-1 backstop (2026-07-13 completed-agents-never-decommission — a
completion was ATTEMPTED but its topic never landed) lives in
``juggle_watchdog_reap_backstop`` (LOC-gate extraction).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from dbops import db_topics as _tp
from dbops import event_kinds as _ek
from dbops.terminal_states import topic_work_landed
from juggle_watchdog_reap_backstop import (
    event_age_secs,
    find_attempted_completion,
    reconcile_failed_landing,
    resolve_min_age_secs,
)
from juggle_watchdog_stall import (
    _capture_pane,
    is_idle_at_prompt,
    markers_for_agent,
)

_log = logging.getLogger("juggle-watchdog")


def reap_completed_agents(
    db: Any,
    mgr: Any,
    *,
    session_id: str = "",
    capture: Callable[[str], str | None] | None = None,
    markers_for: Callable[[dict], tuple[tuple, tuple]] | None = None,
    active_pattern_for: Callable[[dict], str] | None = None,
    backstop_min_age_secs: float | None = None,
) -> None:
    """Release busy agents idling at the prompt whose bound topic already landed.
    Also applies the Fault-1 backstop: an idle agent on a NON-landed topic is
    reconciled (not silently marked done) when a dead-lettered agent_complete
    exists for its thread and is older than ``backstop_min_age_secs`` (env
    ``JUGGLE_REAP_BACKSTOP_MIN_AGE_SECS``, default 300s, when None).
    ``capture``/``markers_for``/``active_pattern_for`` are injectable for tests."""
    if capture is None:
        capture = lambda pid: _capture_pane(mgr, pid)  # noqa: E731
    if markers_for is None:
        markers_for = markers_for_agent
    if active_pattern_for is None:
        from juggle_harness import active_pattern_for_agent as active_pattern_for
    min_age = resolve_min_age_secs(backstop_min_age_secs)

    from juggle_watchdog import _agent_is_non_interactive

    for agent in db.get_all_agents():
        if agent.get("status") != "busy":
            continue
        # One-shot agents have no prompt to idle at; reconcile_oneshot_agents owns them.
        if _agent_is_non_interactive(agent):
            continue
        thread_id = agent.get("assigned_thread")
        if not thread_id:
            continue

        # (ii) bound topic already landed? — the shared signal. An active/ready
        # topic means the agent is legitimately awaiting its next task → skip,
        # UNLESS a completion was actually ATTEMPTED and failed to land (the
        # Fault-1 backstop below) — an active topic with no such evidence is
        # the ordinary "awaiting next task" case and must be left alone.
        topic = _tp.get_topic_by_thread(db, thread_id)
        landed = topic_work_landed(topic)
        dead_letter = None
        if not landed:
            dead_letter = find_attempted_completion(thread_id)
            if dead_letter is None:
                continue
            age = event_age_secs(dead_letter)
            if age is None or age < min_age:
                continue

        # (i) idle at the pane (NOT mid-turn)? A submission marker → still working.
        content = capture(agent.get("pane_id") or "")
        ready, submit = markers_for(agent)
        if not is_idle_at_prompt(content, ready, submit, active_pattern_for(agent)):
            continue

        if landed:
            _release_completed_agent(db, agent, thread_id, session_id)
        else:
            label = _thread_label(db, thread_id, agent["id"])
            reconcile_failed_landing(db, agent, thread_id, label, session_id, dead_letter)


def _release_completed_agent(
    db: Any, agent: dict, thread_id: str, session_id: str
) -> None:
    """Return one completed agent to the pool: record the completion metric,
    close the ledger run, clear stale task state, and emit a routine notice.
    Best-effort; never raises (a cleanup hiccup must not down the tick)."""
    agent_id = agent["id"]
    label = _thread_label(db, thread_id, agent_id)

    # Completion-duration metric (mirror juggle_agent_reap.reap_completed_agent).
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

    # Return to pool + clear stale task state so a reuse doesn't replay it.
    db.update_agent(
        agent_id,
        status="idle",
        assigned_thread=None,
        last_task=None,
        last_send_task_pane_hash=None,
        last_send_task_at=None,
        watchdog_retried=0,
        model=None,
    )

    # Ledger: close any lingering open run for this thread (no-op if already closed).
    try:
        from juggle_agent_reap import close_thread_run_backstop
        close_thread_run_backstop(db, thread_id, output="completed-agent reaped")
    except Exception:
        pass

    # Reconcile a still-'background' thread to done: its topic already landed, so
    # leaving it background would make the orphan detector (check_orphaned_threads,
    # which scans state='background' with no busy agent) re-dispatch the landed
    # work — the very bug this fix closes on the stalled path (fix B).
    try:
        thread = db.get_thread(thread_id)
        if thread and thread.get("state") == "background":
            db.update_thread(thread_id, status="closed")  # status→state maps closed→done
    except Exception:
        pass

    try:
        db.emit_event(
            thread_id=thread_id,
            session_id=session_id,
            kind=_ek.WATCHDOG_RECOVERY,
            message=(f"[Watchdog] [{label}] completed agent idle on a landed topic — "
                     f"released to pool (routine cleanup)"),
        )
    except Exception:
        pass

    _log.info(
        "Watchdog: reaped completed agent %s [%s] — returned to pool",
        agent_id[:8], label,
    )


def release_if_work_landed(
    db: Any, mgr: Any, live: dict, thread_id: str, label: str, session_id: str
) -> bool:
    """Re-dispatch guard (fix B, 2026-07-07 completed-agents-leak) for the
    stalled/crashed recovery path: if the bound topic's work already landed, this
    is a FINISHED topic — decommission the stale pane + agent (free the slot),
    close the ledger run, mark the thread done, and return True so the caller
    skips re-dispatch. NO failure mark / action item. Returns False (work NOT
    landed) so recovery proceeds normally. Best-effort; never raises."""
    try:
        topic = _tp.get_topic_by_thread(db, thread_id)
    except Exception:
        return False
    if not topic_work_landed(topic):
        return False

    agent_id = live["id"]
    try:
        mgr.kill_pane(live["pane_id"])
    except Exception:
        pass
    db.delete_agent(agent_id)
    try:
        from juggle_agent_reap import close_thread_run_backstop
        close_thread_run_backstop(db, thread_id, output="already landed — no re-dispatch")
    except Exception:
        pass
    db.update_thread(thread_id, status="closed")  # status→state maps closed→done
    try:
        db.emit_event(
            thread_id=thread_id, session_id=session_id, kind=_ek.WATCHDOG_RECOVERY,
            message=(f"[Watchdog] [{label}] work already landed — released stale agent, "
                     f"no re-dispatch (routine cleanup)"),
        )
    except Exception:
        pass
    _log.info(
        "Watchdog: skipped re-dispatch for %s — thread %s already landed",
        agent_id[:8], thread_id[:8],
    )
    return True


def _thread_label(db: Any, thread_id: str | None, agent_id: str) -> str:
    if not thread_id:
        return agent_id[:8]
    try:
        thread = db.get_thread(thread_id)
    except Exception:
        thread = None
    if not thread:
        return thread_id[:8]
    return thread.get("user_label") or thread.get("label") or thread_id[:8]
