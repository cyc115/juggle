"""dbops.db_node_machine — unified kind-gated state machine for nodes (P2/P8).

Pure functions only — no DB reads or writes. This is the SOLE transition-decision
engine: the DB wrappers ``db_graph.task_transition`` and
``db_topics.topic_transition`` delegate their (state, event) decision here
(kind='task'); they own no second transition table.
"""
from __future__ import annotations


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
    ("integrating", "integrate_fail"):  "failed-integration",
    ("integrating", "verify_fail"):     "failed-verify",
    ("integrating", "archive"):         "archived",
    # verified
    ("verified", "g1_pass"):  "done",
    ("verified", "archive"):  "archived",
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
        "g1_pass",
    }),
    "research": frozenset({
        "deps_ready", "dep_fail", "reload", "archive",
        "claim", "unready",
        "dispatch", "stale_reset",
        "complete", "exec_fail",
    }),
    "conversation": frozenset({"answer", "archive"}),
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
# §4.3 of the unified-topic-graph spec. Pure lookup — raises KeyError on unknown.

_THREAD_STATUS_TO_NODE_STATE: dict[str, str] = {
    "active":     "open",
    "background": "running",
    "running":    "running",
    "closed":     "done",
    "failed":     "failed-exec",
    "done":       "done",
    "archived":   "archived",
}


def thread_status_to_node_state(status: str) -> str:
    """Map threads.status to the equivalent node.state per spec §4.3.

    Raises KeyError on an unrecognised status value.
    """
    return _THREAD_STATUS_TO_NODE_STATE[status]
