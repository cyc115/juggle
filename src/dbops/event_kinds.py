"""Event kind enum + delivery routing (irl-backbone T1a; irl-envelope T2 adds
``integrate_failed``/``machinery_error``; irl-retry T3 adds
``repair_exhausted``; irl-repair T4 adds ``repair_dispatched``).

Every notifications_v2 row now carries a ``kind`` (one of those below) and
a ``handled_by`` tag describing who needs to see it:

  * ``watchdog``     — the watchdog already auto-handled this; DB row only,
                        never pushed to a human or the orchestrator.
  * ``orchestrator``  — the orchestrator (this Claude Code session) needs to
                        react; pushable into its context.
  * ``user``          — a human needs to know, e.g. via phone push.

``legacy`` (handled_by="") covers pre-T1a emitters not yet converted —
see R1's ``emit_event`` seam.
"""

from __future__ import annotations

LEGACY = "legacy"
WATCHDOG_PROMPT = "watchdog_prompt"
WATCHDOG_STALL = "watchdog_stall"
WATCHDOG_RECOVERY = "watchdog_recovery"
ORCHESTRATOR_VIOLATION = "orchestrator_violation"
AGENT_COMPLETE = "agent_complete"
AGENT_FAILURE = "agent_failure"
AGENT_RECOVERY_DISPATCHED = "agent_recovery_dispatched"
TASK_STATUS = "task_status"
TOPIC_STATUS = "topic_status"
AUTOPILOT_DISPATCH = "autopilot_dispatch"
MANUAL = "manual"
INTEGRATE_FAILED = "integrate_failed"
MACHINERY_ERROR = "machinery_error"
REPAIR_EXHAUSTED = "repair_exhausted"
REPAIR_DISPATCHED = "repair_dispatched"
DISPATCH_FAILED = "dispatch_failed"
RUNNING_ORPHAN = "running_orphan"
# gl-rollup (graph-node learnings, spec §5 + Final revision 3): the watchdog
# tick rolled up learnings written since the last per-project watermark — the
# orchestrator triages them (commands/start.md). Routing derived, not manual.
LEARNINGS_ROLLUP = "learnings_rollup"
# loop-entity V1 Phase 5 (2026-07-04): a scheduled loop's iteration failed (any
# failed/wedged iteration — NOT only a breaker trip) or the failure circuit-breaker
# tripped and paused the loop. A loop failure is machinery / unclassifiable /
# exhausted-repairs, which per the CLAUDE.md triage ladder pushes the orchestrator
# IMMEDIATELY (not a watchdog-only playbook). The loop is a NEW event SOURCE triaged
# under the CURRENT self-heal/triage implementation — routing is derived (no manual
# handled_by), same mechanism as MACHINERY_ERROR / REPAIR_EXHAUSTED / LEARNINGS_ROLLUP.
LOOP_ITERATION_FAILED = "loop_iteration_failed"
LOOP_PAUSED = "loop_paused"

ALL_KINDS = frozenset(
    {
        LEGACY,
        WATCHDOG_PROMPT,
        WATCHDOG_STALL,
        WATCHDOG_RECOVERY,
        ORCHESTRATOR_VIOLATION,
        AGENT_COMPLETE,
        AGENT_FAILURE,
        AGENT_RECOVERY_DISPATCHED,
        TASK_STATUS,
        TOPIC_STATUS,
        AUTOPILOT_DISPATCH,
        MANUAL,
        INTEGRATE_FAILED,
        MACHINERY_ERROR,
        REPAIR_EXHAUSTED,
        REPAIR_DISPATCHED,
        DISPATCH_FAILED,
        RUNNING_ORPHAN,
        LEARNINGS_ROLLUP,
        LOOP_ITERATION_FAILED,
        LOOP_PAUSED,
    }
)

# handled_by="" for legacy mirrors R1's emit_event default (unrouted).
HANDLED_BY = {
    LEGACY: "",
    WATCHDOG_PROMPT: "watchdog",
    WATCHDOG_STALL: "watchdog",
    WATCHDOG_RECOVERY: "watchdog",
    AUTOPILOT_DISPATCH: "watchdog",
    ORCHESTRATOR_VIOLATION: "orchestrator",
    AGENT_COMPLETE: "user",
    AGENT_FAILURE: "user",
    AGENT_RECOVERY_DISPATCHED: "user",
    TASK_STATUS: "orchestrator",
    TOPIC_STATUS: "orchestrator",
    MANUAL: "orchestrator",
    # Defaults only — juggle_integrate_envelope.record_refusal always passes
    # an explicit handled_by: integrate_failed defaults to watchdog but T3's
    # retry policy overrides it to repair_exhausted/orchestrator once the
    # per-signature cap or the 3-total backstop is breached; machinery_error
    # is always orchestrator — never auto-repaired.
    INTEGRATE_FAILED: "watchdog",
    MACHINERY_ERROR: "orchestrator",
    REPAIR_EXHAUSTED: "orchestrator",
    # irl-repair T4: the watchdog dispatched a repair agent into the preserved
    # worktree — FYI only, DB row (never pushed; the orchestrator learns of the
    # outcome via the repair's own integrate result, not this dispatch notice).
    REPAIR_DISPATCHED: "watchdog",
    # df-monitor-dispatch (2026-07-03 dispatch-wedge, defect 3): dispatch/worktree
    # retries and running-orphan recovery are watchdog playbooks — routine attempts
    # stay DB-row-only. On FINAL retry exhaustion or an unrecoverable running-orphan,
    # the emitter overrides handled_by='orchestrator' (triage-ladder escalation), so
    # the monitor pushes it as an event instead of only the action-item feed.
    DISPATCH_FAILED: "watchdog",
    RUNNING_ORPHAN: "watchdog",
    # gl-rollup: the orchestrator reads/triages the rolled-up learnings — the
    # judgment step of the triage ladder. Derived routing (no manual handled_by).
    LEARNINGS_ROLLUP: "orchestrator",
    # loop-entity Phase 5: a loop iteration failure / breaker pause is machinery
    # judgment — the orchestrator triages it immediately (derived routing).
    LOOP_ITERATION_FAILED: "orchestrator",
    LOOP_PAUSED: "orchestrator",
}

# handled_by values that are pushed (to a human or the orchestrator) rather
# than left as a DB row the watchdog already dealt with.
PUSHABLE_HANDLED_BY = frozenset({"orchestrator", "user"})


def handled_by_for_kind(kind: str) -> str:
    """Return the routing tag for ``kind``, defaulting unknown kinds to legacy."""
    return HANDLED_BY.get(kind, HANDLED_BY[LEGACY])


def is_pushable(handled_by: str) -> bool:
    return handled_by in PUSHABLE_HANDLED_BY
