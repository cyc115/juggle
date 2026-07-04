"""dbops.db_node_machine — unified kind-gated state machine for nodes (P2/P8).

Pure functions only — no DB reads or writes. This is the SOLE transition-decision
engine: the DB wrappers ``db_graph.task_transition`` and
``db_topics.topic_transition`` delegate their (state, event) decision here
(kind='task'); they own no second transition table.
"""
from __future__ import annotations

from dbops.node_translation import STATUS_TO_STATE


class InvalidTransition(ValueError):
    """Raised when an event is unknown, state is unknown, or kind disallows it."""


# ── Unified transition table ────────────────────────────────────────────────────
# Keys use the unified node state names. 'open' is the single task-entry state
# (the legacy entry value was retired in P8 — Migration 51 rewrites it on upgrade).

_NODE_TRANSITIONS: dict[tuple[str, str], str] = {
    # open — entry state
    ("open", "deps_ready"):    "ready",
    ("open", "answer"):        "done",        # conversation/decision inline
    ("open", "dep_fail"):      "blocked-failed",
    ("open", "reload"):        "open",
    ("open", "archive"):       "archived",
    # ready
    ("ready", "claim"):        "dispatching",
    ("ready", "dep_fail"):     "blocked-failed",
    ("ready", "reload"):       "open",
    ("ready", "unready"):      "open",
    ("ready", "archive"):      "archived",
    # dispatching
    ("dispatching", "dispatch"):     "running",
    ("dispatching", "stale_reset"):  "ready",
    ("dispatching", "archive"):      "archived",
    # running
    ("running", "integrate_start"):  "integrating",
    ("running", "complete"):         "done",    # research only
    ("running", "exec_fail"):        "failed-exec",
    ("running", "archive"):          "archived",
    # integrating
    ("integrating", "integrate_ok"):    "verified",
    ("integrating", "integrate_submitted"): "integrated-unlanded",
    ("integrating", "integrate_fail"):  "failed-integration",
    ("integrating", "verify_fail"):     "failed-verify",
    ("integrating", "archive"):         "archived",
    # deliver_ok (loop-entity V1 Phase 3, 2026-07-04): delivery='deliver' topics
    # complete to 'delivered' WITHOUT the merged_sha gate — non-merge work (proof
    # is a verify_cmd/attestation, not a landed sha). db_topics.topic_transition
    # only runs the merged_sha gate on the 'verified' target, so 'delivered' never
    # sets merged_sha (verified⟺merged stays the sole merge gate).
    ("integrating", "deliver_ok"):      "delivered",
    # integrated-unlanded (async-land, SPEC §5.1/§5.2): tests-green and submitted
    # to an async land queue (Sapling/git-pr) but not yet an ancestor of trunk.
    # Sits BELOW verified — must never set merged_sha (graph_guards.topic_is_merged
    # stays the sole verified⟺merged gate; the land-poller re-checks and promotes).
    ("integrated-unlanded", "land_confirmed"): "verified",
    ("integrated-unlanded", "land_fail"):      "failed-integration",
    ("integrated-unlanded", "archive"):        "archived",
    # verified
    ("verified", "g1_pass"):  "done",
    ("verified", "archive"):  "archived",
    # delivered — non-merge success terminal (Phase 3). Mirrors verified's exits
    # (g1_pass→done, archive→archived, reopen→open) so a delivered node is never a
    # dead-end and add-task can reopen a delivered topic. NEVER sets merged_sha.
    ("delivered", "g1_pass"): "done",
    ("delivered", "archive"): "archived",
    ("delivered", "reopen"):  "open",
    # failure terminals → reload resurrects
    ("failed-exec",         "reload"):  "open",
    ("failed-exec",         "archive"): "archived",
    ("failed-integration",  "reload"):  "open",
    ("failed-integration",  "archive"): "archived",
    ("failed-verify",       "reload"):  "open",
    ("failed-verify",       "archive"): "archived",
    ("blocked-failed",      "reload"):  "open",
    ("blocked-failed",      "archive"): "archived",
    # done → archive only
    ("done", "archive"): "archived",
    # cancelled — soft terminal (P2 graph-node primitives, DA-B4). Reachable via
    # a 'cancel' event from any NON-in-flight state (PROTECTED_STATES —
    # dispatching/running/integrating/verified — are refused: no table entry).
    # 'cancelled' is a COMPLETION terminal, never a blocking/failed one.
    ("open",                 "cancel"): "cancelled",
    ("ready",                "cancel"): "cancelled",
    ("integrated-unlanded",  "cancel"): "cancelled",
    ("failed-exec",          "cancel"): "cancelled",
    ("failed-integration",   "cancel"): "cancelled",
    ("failed-verify",        "cancel"): "cancelled",
    ("blocked-failed",       "cancel"): "cancelled",
    ("background",           "cancel"): "cancelled",
    ("done",                 "cancel"): "cancelled",
    # un-cancel: retry-node reuses the existing 'reload' event → open (DA "un-cancel").
    ("cancelled",            "reload"):  "open",
    ("cancelled",            "archive"): "archived",
    # reopen — topic-only (T-fix-addtask-reopen-verified-topic, 2026-07-03):
    # add-task into a topic terminal-parked with old work must reopen it, else
    # a newly attached task can never dispatch (topic_ready_eligible requires
    # state='open'). 'verified' has no 'reload' entry (task-level reload never
    # resurrects a verified task — validate_add_task refuses that re-add before
    # it gets here), so topics get their own event instead of overloading it.
    ("verified",             "reopen"): "open",
    ("failed-exec",          "reopen"): "open",
    ("failed-integration",   "reopen"): "open",
    ("failed-verify",        "reopen"): "open",
    ("blocked-failed",       "reopen"): "open",
    ("integrated-unlanded",  "reopen"): "open",
    # background — conversation agent dispatched in the background (R2-1).
    # Kept DISTINCT from 'running' so the watchdog reaper + cockpit 2a/2b stay correct.
    ("open",       "dispatch_bg"): "background",
    ("background", "foreground"):  "open",        # agent completes / user resumes -> active
    ("background", "answer"):      "done",        # bg agent answered/closed the conversation
    ("background", "archive"):     "archived",
}

# ── Kind-legal event sets ───────────────────────────────────────────────────────
# An event absent from a kind's set raises InvalidTransition regardless of
# whether the base _NODE_TRANSITIONS entry exists.

_KIND_LEGAL: dict[str, frozenset[str]] = {
    "task": frozenset({
        "deps_ready", "dep_fail", "reload", "archive",
        "claim", "unready",
        "dispatch", "stale_reset",
        "integrate_start", "exec_fail",
        "integrate_ok", "integrate_fail", "verify_fail",
        "integrate_submitted", "land_confirmed", "land_fail",
        "deliver_ok",
        "g1_pass", "reopen", "cancel",
    }),
    "research": frozenset({
        "deps_ready", "dep_fail", "reload", "archive",
        "claim", "unready",
        "dispatch", "stale_reset",
        "complete", "exec_fail",
    }),
    "conversation": frozenset({"answer", "archive", "dispatch_bg", "foreground"}),
    "decision":     frozenset({"answer", "archive"}),
}


def node_transition(state: str, event: str, kind: str) -> str:
    """Pure kind-gated state transition for unified nodes.

    Returns new_state. Raises InvalidTransition on unknown state/event or
    when the event is illegal for the given kind.
    """
    key = (state, event)
    if key not in _NODE_TRANSITIONS:
        raise InvalidTransition(
            f"no transition defined: state={state!r} event={event!r}"
        )
    legal = _KIND_LEGAL.get(kind)
    if legal is None:
        raise InvalidTransition(f"unknown node kind: {kind!r}")
    if event not in legal:
        raise InvalidTransition(
            f"event {event!r} is not legal for kind={kind!r} "
            f"(state={state!r})"
        )
    return _NODE_TRANSITIONS[key]


def legal_events(kind: str) -> frozenset[str]:
    """Events legal for a node kind (raises InvalidTransition on unknown kind).

    The legacy DB wrappers (db_graph/db_topics) reuse this to fail loud on an
    unknown event BEFORE looking up the (state, event) transition.
    """
    legal = _KIND_LEGAL.get(kind)
    if legal is None:
        raise InvalidTransition(f"unknown node kind: {kind!r}")
    return legal


# ── threads.status → node.state mapping ────────────────────────────────────────
# §4.3 of the unified-topic-graph spec. The forward value-map lives in exactly ONE
# place — dbops.node_translation.STATUS_TO_STATE (P8 H1); this is a thin delegator.


def thread_status_to_node_state(status: str) -> str:
    """Map threads.status to the equivalent node.state per spec §4.3.

    Delegates to the canonical forward map; raises KeyError on an unrecognised
    status value.
    """
    return STATUS_TO_STATE[status]
