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
    ("background", "running"),
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


# ── back-compat: existing _TRANSITIONS still intact ────────────────────────────

def test_existing_transitions_still_work():
    """Existing db_graph._TRANSITIONS unchanged — import and spot-check."""
    from dbops.db_graph import _TRANSITIONS as OLD

    assert OLD[("pending", "deps_ready")] == "ready"
    assert OLD[("ready", "claim")] == "dispatching"
    assert OLD[("running", "integrate_start")] == "integrating"
    assert OLD[("integrating", "integrate_ok")] == "verified"


def test_existing_topic_transition_unchanged():
    """topic_transition still works via db_graph imports (no regression)."""
    from dbops.db_graph import _TRANSITIONS

    # Sample a few topic transitions that go through the same machine
    assert _TRANSITIONS[("dispatching", "dispatch")] == "running"
    assert _TRANSITIONS[("failed-exec", "reload")] == "pending"


# ── _NODE_TRANSITIONS completeness check ───────────────────────────────────────

def test_node_transitions_has_archive_from_every_non_archived_state():
    """Every non-archived state must have an archive edge."""
    non_archived = {s for (s, _) in _NODE_TRANSITIONS if s != "archived"}
    for state in non_archived:
        assert ("archive" in {e for (st, e) in _NODE_TRANSITIONS if st == state}), \
            f"state {state!r} has no archive edge"
