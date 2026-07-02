"""juggle_watchdog_tickguard — tick-hang defenses (T-fix-watchdog-tick-lock-hang).

INCIDENT 2026-07-02: the watchdog's first tick hung 10+ minutes — `sample`
showed the main thread parked forever in PyThread_acquire_lock. The eager
summary-cache warmer (T-summary-eager-gen) ran synchronously inside
`_poll_once`, so any internal stall there blocked the whole tick and, since
the main loop never returned to `_tick_event.wait()`, every subsequent tick's
dispatch/advance/drain too.

Owns two independent defenses, both extracted here purely to keep
juggle_watchdog_daemon.py under its LOC budget:

1. ``dispatch_summary_warming`` — runs the summary warmer fire-and-forget on a
   background thread guarded by a non-blocking busy-skip lock, so a hang
   inside it can never stall ``_poll_once`` again.
2. ``arm_tick_watchdog`` — a watchdog-of-the-watchdog: a background timer that
   logs ERROR + dumps every thread's stack if a tick doesn't return within a
   budget, so a hang is diagnosable from logs alone (no live `sample` needed).

Must not own tick body composition or dispatch policy — those stay in
juggle_watchdog_daemon.py.
"""
from __future__ import annotations

import faulthandler
import logging
import sys
import threading

_log = logging.getLogger("juggle.watchdog")

# Summary-cache warming busy-guard: non-blocking — acquire(blocking=False)
# either wins and a background thread runs the sweep, or a sweep is already
# in flight and this tick just skips. Never waited on.
_warming_lock = threading.Lock()


def dispatch_summary_warming(db) -> None:
    """Fire-and-forget the (i)-pane summary cache warmer; never blocks the tick.

    A hang anywhere inside warm_stale_summaries (LLM call, lock, join) stays
    confined to this daemon thread — it can no longer stall _poll_once or any
    later tick. If a previous sweep is still running, this tick just skips.
    """
    if not _warming_lock.acquire(blocking=False):
        _log.info("Watchdog: summary warming still in flight from a prior tick — skipping")
        return

    def _worker():
        try:
            from juggle_summary_warmer import warm_stale_summaries
            warm_stale_summaries(db)
        except Exception:
            _log.exception("Watchdog: summary warming tick failed — continuing")
        finally:
            _warming_lock.release()

    threading.Thread(target=_worker, name="juggle-summary-warmer", daemon=True).start()


# A tick that runs longer than this multiplier of the poll interval is
# treated as hung — the 2026-07-02 incident ran 10+ minutes vs a 30s interval
# with nothing in the logs to diagnose it.
TICK_BUDGET_MULTIPLIER = 3


def _dump_tick_hang_stack(budget_secs: float) -> None:
    _log.error(
        "Watchdog: tick exceeded budget of %.0fs — dumping thread stacks (hang suspected)",
        budget_secs,
    )
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def arm_tick_watchdog(interval: int, multiplier: float = TICK_BUDGET_MULTIPLIER) -> threading.Timer:
    """Start a timer that dumps stacks if not cancelled within budget.

    Caller MUST cancel() the returned timer once its tick returns normally —
    this only fires when a tick has NOT returned in time.
    """
    budget = interval * multiplier
    timer = threading.Timer(budget, _dump_tick_hang_stack, args=(budget,))
    timer.daemon = True
    timer.start()
    return timer
