"""Event kind enum + delivery routing (irl-backbone T1a).

Every notifications_v2 row now carries a ``kind`` (one of the 12 below) and
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
}

# handled_by values that are pushed (to a human or the orchestrator) rather
# than left as a DB row the watchdog already dealt with.
PUSHABLE_HANDLED_BY = frozenset({"orchestrator", "user"})


def handled_by_for_kind(kind: str) -> str:
    """Return the routing tag for ``kind``, defaulting unknown kinds to legacy."""
    return HANDLED_BY.get(kind, HANDLED_BY[LEGACY])


def is_pushable(handled_by: str) -> bool:
    return handled_by in PUSHABLE_HANDLED_BY
