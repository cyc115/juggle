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

    # Loop-fire tick (loop-entity Phase 5): fire every due active loop (CAS-safe,
    # overlap-skip, circuit-breaker). Guarded so a loop bug never downs the tick.
    try:
        from juggle_loop_fire import fire_due_loops

        fire_due_loops(db, session_id)
    except Exception:
        _log.exception("Watchdog: loop-fire tick failed — continuing")
