"""juggle_watchdog_loop_tick — graph-dispatch tick substep extracted from the
watchdog daemon.

Extraction rationale (architecture LOC gate): ``juggle_watchdog_daemon`` sits at
its 487-line allowlist budget, so the graph-dispatch tick block was lifted here to
free budget (and to give the co-located loop-fire step a home). Behaviour-neutral —
the daemon now calls ``run_graph_and_loop_ticks`` in the same tick position.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("juggle.watchdog.loop_tick")


def run_graph_and_loop_ticks(db, mgr, session_id: str) -> None:
    """Graph claim-dispatch tick (autopilot Phase 2). Guarded so a bug never downs
    the daemon."""
    try:
        from juggle_graph_dispatch import graph_tick

        graph_tick(db, mgr)
        from juggle_graph_repair import run_tick_sweeps  # T1c: TTL + notif reconcile

        run_tick_sweeps(db)
    except Exception:
        _log.exception("Watchdog: graph dispatch tick failed — continuing")
