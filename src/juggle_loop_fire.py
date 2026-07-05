"""juggle_loop_fire — CAS-safe watchdog loop firing + failure circuit-breaker +
never-swallow iteration-failure surfacing (loop-entity V1, Phase 5, 2026-07-04).

The watchdog tick calls ``fire_due_loops`` every poll. For each ``active`` loop
whose ``next_run`` is due it:

  1. **Advances next_run via CAS BEFORE instantiating** (mirrors dbops.state_write.
     cas_state): the winning tick owns the window; a crash/retry between fire and
     advance can never double-fire (critique §Axis-7b). The singleton flock already
     serializes tick-vs-tick; CAS covers crash-between-fire-and-advance.
  2. **Evaluates the PRIOR iteration's outcome** (a loop iteration is a template
     graph that runs over many ticks; its success/failure is only known once its
     nodes reach a terminal state):
       * still in-flight  → OVERLAP: skip + log; K consecutive skips ESCALATE
                            (never skip-forever — §Axis-2 Objection B).
       * failed           → surface (all three, below) + bump the breaker counter;
                            at ``max_consecutive_failures`` → pause + escalate.
       * success/first    → reset the breaker counter, then fire the next iteration.
  3. **Instantiates the next iteration** (run-seq namespaced) as fresh nodes.

**Never-swallow failure surfacing (user directive 2026-07-04 — LOAD-BEARING).**
``surface_iteration_failure`` is the SINGLE choke point every iteration failure
flows through; it NEVER swallows the error state and does all three, reusing
existing primitives:
  (a) ``db.emit_event(kind=LOOP_ITERATION_FAILED)`` — a notifications_v2 row
      carrying the ACTUAL non-empty error text, derived-routed to ``orchestrator``
      (this row is BOTH the error-carrying notification AND the orchestrator push).
  (b) ``db.add_action_item_once(..., priority='high')`` — a HIGH action item on
      the loop's thread, visible in the cockpit action pane (deduped so a loop
      failing every tick does not flood the pane).
  (c) the orchestrator push — the (a) event's ``handled_by='orchestrator'`` routing
      (same mechanism as MACHINERY_ERROR / REPAIR_EXHAUSTED / LEARNINGS_ROLLUP).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from dbops import event_kinds as _ek
from dbops.schema import MAX_LOOP_THREADS as _DEFAULT_MAX_LOOP_THREADS
from dbops.schema import _now as _now_ts
from dbops.terminal_states import FAILURE_TERMINAL_STATES, TERMINAL_STATES
from juggle_loop_regen import _fire_next_iteration_atomic

_log = logging.getLogger("juggle.loop_fire")

# Disjoint loop-thread budget (loop-entity V2 §8, P5b) — bound here as a module
# global (mirrors dbops.threads.MAX_THREADS) so tests can patch it via
# ``juggle_loop_fire.MAX_LOOP_THREADS``. Firing concurrency stays gated by
# MAX_BACKGROUND_AGENTS; this only caps the number of concurrently-LIVE loops.
MAX_LOOP_THREADS: int = _DEFAULT_MAX_LOOP_THREADS

# In-process consecutive-overlap-skip tracker (keyed by loop id). The watchdog is a
# long-running process so this persists across ticks (same pattern as the daemon's
# _stall_tracker / _enter_sent). K skips → escalate + reset. A wedged iteration must
# surface, never silently kill the loop by skipping forever (§Axis-2 Objection B).
_SKIP_COUNTS: dict[str, int] = {}


def reset_skip_tracker() -> None:
    """Clear the in-process overlap-skip tracker (test isolation / daemon restart)."""
    _SKIP_COUNTS.clear()


def max_skips() -> int:
    """K — consecutive overlap-skips before escalating a wedged iteration."""
    try:
        return max(1, int(os.environ.get("JUGGLE_LOOP_MAX_SKIPS", "3")))
    except ValueError:
        return 3


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def iteration_outcome(db, project_id: str, loop_id: str, seq: int) -> tuple[str, str]:
    """Classify iteration ``seq`` of ``loop_id`` from its task nodes' states.

    Returns ``(status, detail)`` where status is one of ``in_flight`` / ``failed`` /
    ``success`` and ``detail`` names the failed node(s)/state(s) (non-empty only for
    a failure — that string is the surfaced error text). An iteration with no task
    nodes reads ``success`` (nothing to wait on)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, state FROM nodes WHERE kind='task' AND project_id=? AND id LIKE ?",
            (project_id, f"{loop_id}-r{seq}-%"),
        ).fetchall()
    tasks = [(r[0], r[1]) for r in rows]
    if not tasks:
        return ("success", "")
    if any(state not in TERMINAL_STATES for _, state in tasks):
        return ("in_flight", "")
    failed = [(nid, state) for nid, state in tasks if state in FAILURE_TERMINAL_STATES]
    if failed:
        return ("failed", ", ".join(f"{nid}={state}" for nid, state in failed))
    return ("success", "")


def count_live_loop_pool(db) -> int:
    """Occupancy of the DISJOINT loop-thread pool (loop-entity V2 §8, P5b): the number
    of ``active`` loops currently running an in-flight (non-terminal) current-generation
    iteration — i.e. each holding one live loop-thread slot right now.

    Bounded by ``MAX_LOOP_THREADS``, held SEPARATE from the interactive ``MAX_THREADS``
    conversation pool: firing a loop iteration instantiates task nodes under the loop's
    stable topic and reuses its bound thread, so it allocates ZERO interactive slots.
    A loop whose current generation is TERMINAL (parked between fires) frees its slot; a
    loop with a running generation (incl. a freshly-created loop's ``r0``) occupies one."""
    live = 0
    for lp in db.list_active_loops():
        status, _ = iteration_outcome(db, lp["project_id"], lp["id"], lp["run_seq"])
        if status == "in_flight":
            live += 1
    return live


def surface_iteration_failure(db, loop: dict, session_id: str, *, err: str,
                              seq: int) -> None:
    """The single never-swallow choke point for ANY loop iteration failure.

    Does all three (emit orchestrator event with the real error, file a HIGH action
    item, push the orchestrator) reusing existing primitives — and swallows NOTHING.
    """
    loop_id = loop["id"]
    thread_id = loop.get("thread_id")
    detail = err.strip() or f"iteration r{seq} failed (no detail)"  # NEVER empty
    # (a)+(c): orchestrator-routed notification carrying the actual error state. The
    # LOOP_ITERATION_FAILED kind derives handled_by='orchestrator', so this one row
    # is BOTH the error-carrying notifications_v2 event and the orchestrator push.
    db.emit_event(
        thread_id=thread_id,
        message=f"[Loop {loop_id}] iteration r{seq} failed: {detail}",
        session_id=session_id,
        kind=_ek.LOOP_ITERATION_FAILED,
    )
    # (b): a HIGH action item on the loop's thread. Message is loop-scoped + STABLE
    # (no per-iteration seq) so add_action_item_once dedups a repeatedly-failing loop
    # to ONE open item instead of stacking one per tick — while (a) still re-pushes
    # the orchestrator every failure.
    db.add_action_item_once(
        thread_id,
        f"Loop {loop_id} iteration failing — needs attention",
        "loop_failure",
        priority="high",
    )
    _log.warning("Loop %s iteration r%d failed: %s", loop_id, seq, detail)


def _handle_overlap(db, loop: dict, session_id: str, seq: int) -> str:
    """Prior iteration still in-flight → skip this window; escalate after K skips."""
    loop_id = loop["id"]
    n = _SKIP_COUNTS.get(loop_id, 0) + 1
    _SKIP_COUNTS[loop_id] = n
    _log.info("Loop %s overlap-skip #%d (iteration r%d still in-flight)", loop_id, n, seq)
    if n >= max_skips():
        # A wedged iteration that never terminates is a machinery/judgment event —
        # surface it through the same never-swallow choke point (orchestrator push).
        surface_iteration_failure(
            db, loop, session_id,
            err=f"prior iteration r{seq} still in-flight after {n} skips (wedged)",
            seq=seq,
        )
        _SKIP_COUNTS[loop_id] = 0
        return "escalated"
    return "skipped"


def _fire_one(db, loop: dict, session_id: str, now: str) -> str:
    """Fire (or skip/escalate/pause) a single due loop. Returns the action taken."""
    loop_id = loop["id"]
    cadence = loop["cadence"]
    expected = loop["next_run"]

    # Advance next_run via CAS BEFORE any instantiation (crash/double-fire guard).
    from juggle_cmd_loop_create import compute_next_run
    new_next = compute_next_run(cadence, now=_parse(expected))
    if db.advance_next_run_cas(loop_id, expected=expected, new=new_next) != 1:
        return "raced"  # another tick/process already claimed this window

    loop = db.get_loop(loop_id)  # reload fresh (run_seq, counters)
    prior_seq = loop["run_seq"]
    status, detail = iteration_outcome(db, loop["project_id"], loop_id, prior_seq)

    if status == "in_flight":
        return _handle_overlap(db, loop, session_id, prior_seq)

    # Loop-pool budget (loop-entity V2 §8, P5b): the prior generation is terminal, so we
    # WOULD fire a new one now — gate on the DISJOINT loop pool BEFORE any outcome side
    # effects. At cap → defer the ENTIRE fire (INCLUDING surfacing the prior outcome + the
    # breaker bump) to a window with room; next_run stays already-advanced (benign
    # backpressure, like an overlap-skip). Deferring BEFORE the breaker bump is
    # load-bearing: processing a FAILED prior here without advancing run_seq would
    # re-surface + re-bump the SAME failed generation every window and spuriously pause the
    # loop under sustained saturation. Bounds concurrently-live loops WITHOUT touching the
    # interactive MAX_THREADS; firing stays gated by MAX_BACKGROUND_AGENTS.
    live = count_live_loop_pool(db)
    if live >= MAX_LOOP_THREADS:
        _log.info("Loop %s fire deferred — loop pool at cap (%d live >= %d)",
                  loop_id, live, MAX_LOOP_THREADS)
        return "pool_full"

    if status == "failed":
        surface_iteration_failure(db, loop, session_id, err=detail, seq=prior_seq)
        n = db.bump_consecutive_failures(loop_id)
        if n >= loop["max_consecutive_failures"]:
            db.pause_loop(loop_id)
            db.emit_event(
                thread_id=loop.get("thread_id"),
                message=(f"[Loop {loop_id}] circuit-breaker tripped — paused after "
                         f"{n} consecutive failed iterations"),
                session_id=session_id,
                kind=_ek.LOOP_PAUSED,
            )
            _log.warning("Loop %s paused (breaker: %d consecutive failures)", loop_id, n)
            return "paused"
    else:  # success (or first fire with no prior work)
        db.reset_consecutive_failures(loop_id)

    _SKIP_COUNTS[loop_id] = 0  # a real fire clears the overlap-skip streak
    new_seq = _fire_next_iteration_atomic(db, loop)  # seq bump + instantiate atomic
    db.stamp_last_run(loop_id)
    _log.info("Loop %s fired iteration r%d", loop_id, new_seq)
    return "fired"


def fire_due_loops(db, session_id: str, now: str | None = None) -> list[tuple[str, str]]:
    """Fire every ``active`` loop whose ``next_run`` is due. Returns ``[(loop_id,
    action)]``. A tick with no due loop is a pure no-op (preserves non-loop tick
    behaviour). Per-loop failures are isolated so one bad loop never downs the tick."""
    now = now or _now_ts()
    results: list[tuple[str, str]] = []
    for loop in db.list_active_loops():
        nr = loop.get("next_run")
        if not nr or nr > now:
            continue  # not due (or never scheduled)
        try:
            results.append((loop["id"], _fire_one(db, loop, session_id, now)))
        except Exception as exc:
            # A fire-step machinery error (reconstruct/instantiate/DB failure) is a
            # machinery-error class event — per the triage ladder it pushes the
            # orchestrator IMMEDIATELY and must NEVER be swallowed to a log line
            # (code-review #1, user never-swallow directive). Route it through the
            # same choke point; guard so surfacing itself can't down the tick.
            _log.exception("Loop %s fire failed — surfacing", loop.get("id"))
            try:
                surface_iteration_failure(
                    db, loop, session_id,
                    err=f"loop fire machinery error: {exc}",
                    seq=loop.get("run_seq", -1),
                )
            except Exception:
                _log.exception("Loop %s failure-surface ALSO failed — continuing",
                               loop.get("id"))
            results.append((loop.get("id"), "error"))
    return results
