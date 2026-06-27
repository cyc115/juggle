"""Regression-pinned tests for the unified node_transition function (P2).

2026-06-20: kind-gated state machine folding threads.status into unified nodes.
"""
from __future__ import annotations

import pytest

from dbops.db_node_machine import (
    InvalidTransition,
    node_transition,
    thread_status_to_node_state,
    _NODE_TRANSITIONS,
)


# ── legal transitions per kind (parametrize) ───────────────────────────────────

TASK_LEGAL_EDGES = [
    # (from_state, event, expected_to)
    ("open",         "deps_ready",    "ready"),
    ("open",         "dep_fail",      "blocked-failed"),
    ("open",         "reload",        "open"),
    ("open",         "archive",       "archived"),
    ("ready",        "claim",         "dispatching"),
    ("ready",        "dep_fail",      "blocked-failed"),
    ("ready",        "reload",        "open"),
    ("ready",        "unready",       "open"),
    ("ready",        "archive",       "archived"),
    ("dispatching",  "dispatch",      "running"),
    ("dispatching",  "stale_reset",   "ready"),
    ("dispatching",  "archive",       "archived"),
    ("running",      "integrate_start", "integrating"),
    ("running",      "exec_fail",     "failed-exec"),
    ("running",      "archive",       "archived"),
    ("integrating",  "integrate_ok",  "verified"),
    ("integrating",  "integrate_fail","failed-integration"),
    ("integrating",  "verify_fail",   "failed-verify"),
    ("integrating",  "archive",       "archived"),
    ("verified",     "g1_pass",       "done"),
    ("verified",     "archive",       "archived"),
    ("failed-exec",  "reload",        "open"),
    ("failed-integration", "reload",  "open"),
    ("failed-verify","reload",        "open"),
    ("blocked-failed","reload",       "open"),
]

RESEARCH_LEGAL_EDGES = [
    ("open",         "deps_ready",    "ready"),
    ("open",         "dep_fail",      "blocked-failed"),
    ("open",         "reload",        "open"),
    ("open",         "archive",       "archived"),
    ("ready",        "claim",         "dispatching"),
    ("ready",        "dep_fail",      "blocked-failed"),
    ("ready",        "reload",        "open"),
    ("ready",        "unready",       "open"),
    ("ready",        "archive",       "archived"),
    ("dispatching",  "dispatch",      "running"),
    ("dispatching",  "stale_reset",   "ready"),
    ("dispatching",  "archive",       "archived"),
    ("running",      "complete",      "done"),
    ("running",      "exec_fail",     "failed-exec"),
    ("running",      "archive",       "archived"),
    ("failed-exec",  "reload",        "open"),
    ("blocked-failed","reload",       "open"),
]

CONVERSATION_LEGAL_EDGES = [
    ("open",  "answer",  "done"),
    ("open",  "archive", "archived"),
    ("done",  "archive", "archived"),
]

DECISION_LEGAL_EDGES = [
    ("open",  "answer",  "done"),
    ("open",  "archive", "archived"),
    ("done",  "archive", "archived"),
]


@pytest.mark.parametrize("state,event,expected", TASK_LEGAL_EDGES)
def test_task_legal_transitions(state, event, expected):
    assert node_transition(state, event, "task") == expected


@pytest.mark.parametrize("state,event,expected", RESEARCH_LEGAL_EDGES)
def test_research_legal_transitions(state, event, expected):
    assert node_transition(state, event, "research") == expected


@pytest.mark.parametrize("state,event,expected", CONVERSATION_LEGAL_EDGES)
def test_conversation_legal_transitions(state, event, expected):
    assert node_transition(state, event, "conversation") == expected


@pytest.mark.parametrize("state,event,expected", DECISION_LEGAL_EDGES)
def test_decision_legal_transitions(state, event, expected):
    assert node_transition(state, event, "decision") == expected


# ── illegal transitions per kind ───────────────────────────────────────────────

def test_conversation_cannot_deps_ready():
    with pytest.raises(InvalidTransition):
        node_transition("open", "deps_ready", "conversation")


def test_conversation_cannot_integrating():
    with pytest.raises(InvalidTransition):
        node_transition("running", "integrate_start", "conversation")


def test_conversation_cannot_verified():
    with pytest.raises(InvalidTransition):
        node_transition("integrating", "integrate_ok", "conversation")


def test_decision_cannot_deps_ready():
    with pytest.raises(InvalidTransition):
        node_transition("open", "deps_ready", "decision")


def test_decision_cannot_integrating():
    with pytest.raises(InvalidTransition):
        node_transition("running", "integrate_start", "decision")


def test_research_cannot_integrate_start():
    with pytest.raises(InvalidTransition):
        node_transition("running", "integrate_start", "research")


def test_research_cannot_g1_pass():
    with pytest.raises(InvalidTransition):
        node_transition("verified", "g1_pass", "research")


def test_task_cannot_answer():
    with pytest.raises(InvalidTransition):
        node_transition("open", "answer", "task")


def test_task_cannot_complete():
    with pytest.raises(InvalidTransition):
        node_transition("running", "complete", "task")


# ── undefined state/event → InvalidTransition ──────────────────────────────────

def test_unknown_event_raises():
    with pytest.raises(InvalidTransition):
        node_transition("open", "nonexistent_event", "task")


def test_unknown_state_raises():
    with pytest.raises(InvalidTransition):
        node_transition("nonexistent_state", "deps_ready", "task")


# ── threads.status → node.state mapping ────────────────────────────────────────

@pytest.mark.parametrize("status,expected_state", [
    ("active",     "open"),
    ("background", "background"),
    ("running",    "running"),
    ("closed",     "done"),
    ("failed",     "failed-exec"),
    ("done",       "done"),
    ("archived",   "archived"),
])
def test_thread_status_to_node_state(status, expected_state):
    assert thread_status_to_node_state(status) == expected_state


def test_thread_status_unknown_raises():
    with pytest.raises(KeyError):
        thread_status_to_node_state("bogus_status")


def test_background_state_accepted_by_machine():
    """2026-06-27 P8 R2-1: the unified machine must accept the 'background' state for
    conversation nodes — it must NOT raise InvalidTransition on a live state (the C3
    failure mode, now for background)."""
    assert node_transition("open", "dispatch_bg", "conversation") == "background"
    assert node_transition("background", "foreground", "conversation") == "open"
    assert node_transition("background", "archive", "conversation") == "archived"


# ── single-engine invariant (P8 C1): db_graph owns no second transition table ──

def test_db_graph_has_no_local_transition_table():
    """2026-06-27 P8 C1: the duplicate db_graph._TRANSITIONS/_EVENTS tables are
    DELETED — db_graph.task_transition delegates its decision to node_transition.
    Every transition these once pinned is covered by test_task_legal_transitions."""
    import dbops.db_graph as g
    assert not hasattr(g, "_TRANSITIONS"), "duplicate transition table must be deleted"
    assert not hasattr(g, "_EVENTS"), "duplicate event set must be deleted"


# ── _NODE_TRANSITIONS completeness check ───────────────────────────────────────

def test_node_transitions_has_archive_from_every_non_archived_state():
    """Every non-archived state must have an archive edge."""
    non_archived = {s for (s, _) in _NODE_TRANSITIONS if s != "archived"}
    for state in non_archived:
        assert ("archive" in {e for (st, e) in _NODE_TRANSITIONS if st == state}), \
            f"state {state!r} has no archive edge"
