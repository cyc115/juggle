"""juggle_watchdog_sweeps — the per-tick agent-health sweep driver.

Groups the two busy-agent sweeps the daemon runs each tick, each independently
guarded so a bug in one never downs the tick (or the other):

  1. STALL nudge   (juggle_watchdog_stall.check_stalled_agents) — a busy agent
     idling at the prompt mid-task is nudged to finalize, then escalated.
  2. COMPLETED reap (juggle_watchdog_reap_done.reap_completed_agents) — a busy
     agent idle at the prompt whose bound topic already LANDED is returned to the
     pool as routine cleanup (2026-07-07 completed-agents-leak).

Order matters: stall runs first (it may nudge an agent that is genuinely still
finalizing); the reaper only acts on agents whose topic is already terminal, so
the two never contend for the same agent.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("juggle-watchdog")


def run_agent_health_sweeps(
    db: Any,
    mgr: Any,
    stall_tracker: Any,
    *,
    now: float,
    session_id: str = "",
) -> None:
    """Run both busy-agent sweeps for one tick, each guarded independently."""
    try:
        from juggle_watchdog_stall import check_stalled_agents
        check_stalled_agents(db, mgr, stall_tracker, now=now, session_id=session_id)
    except Exception:
        _log.exception("Watchdog: stall detector tick failed — continuing")

    try:
        from juggle_watchdog_reap_done import reap_completed_agents
        reap_completed_agents(db, mgr, session_id=session_id)
    except Exception:
        _log.exception("Watchdog: completed-agent reaper tick failed — continuing")
