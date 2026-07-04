"""Tests for irl-backbone T1a: event kinds enum + delivery routing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops import event_kinds as ek


def test_kinds_defined():
    """irl-envelope T2 adds integrate_failed + machinery_error to T1a's 12;
    irl-retry T3 adds repair_exhausted; irl-repair T4 adds repair_dispatched;
    df-monitor-dispatch adds dispatch_failed + running_orphan; gl-rollup adds
    learnings_rollup; loop-entity Phase 5 adds loop_iteration_failed + loop_paused."""
    assert len(ek.ALL_KINDS) == 21


def test_loop_failure_kinds_route_orchestrator():
    """loop-entity Phase 5 (2026-07-04): a loop iteration failure / breaker pause
    pushes the orchestrator immediately (derived routing, pushable) — a loop failure
    is machinery/judgment per the CLAUDE.md triage ladder, never DB-row-only."""
    for kind in (ek.LOOP_ITERATION_FAILED, ek.LOOP_PAUSED):
        assert ek.handled_by_for_kind(kind) == "orchestrator"
        assert ek.is_pushable(ek.handled_by_for_kind(kind))


def test_dispatch_failure_kinds_default_watchdog_overridable_to_orchestrator():
    """df-monitor-dispatch (2026-07-03 dispatch-wedge, defect 3): routine dispatch
    retries + orphan recovery stay DB-row-only (watchdog default); the emitter
    escalates handled_by='orchestrator' on exhaustion so the monitor pushes it."""
    for kind in (ek.DISPATCH_FAILED, ek.RUNNING_ORPHAN):
        assert ek.handled_by_for_kind(kind) == "watchdog"
        assert not ek.is_pushable(ek.handled_by_for_kind(kind))
        assert ek.is_pushable("orchestrator")  # override path pushes


def test_repair_dispatched_is_watchdog_not_pushable():
    """T4: the repair-dispatch notice is FYI-only (DB row), never pushed —
    the orchestrator learns the outcome from the repair's own integrate result."""
    assert ek.handled_by_for_kind(ek.REPAIR_DISPATCHED) == "watchdog"
    assert not ek.is_pushable(ek.handled_by_for_kind(ek.REPAIR_DISPATCHED))


def test_integrate_failed_is_orchestrator_pushable_watchdog_default():
    assert ek.handled_by_for_kind(ek.INTEGRATE_FAILED) == "watchdog"
    assert not ek.is_pushable(ek.handled_by_for_kind(ek.INTEGRATE_FAILED))


def test_machinery_error_is_always_orchestrator_pushable():
    assert ek.handled_by_for_kind(ek.MACHINERY_ERROR) == "orchestrator"
    assert ek.is_pushable(ek.handled_by_for_kind(ek.MACHINERY_ERROR))


def test_repair_exhausted_is_orchestrator_pushable():
    assert ek.handled_by_for_kind(ek.REPAIR_EXHAUSTED) == "orchestrator"
    assert ek.is_pushable(ek.handled_by_for_kind(ek.REPAIR_EXHAUSTED))


def test_every_kind_has_a_handled_by():
    for kind in ek.ALL_KINDS:
        assert ek.handled_by_for_kind(kind) in ("", "watchdog", "orchestrator", "user")


def test_watchdog_kinds_are_not_pushable():
    for kind in (
        ek.WATCHDOG_PROMPT,
        ek.WATCHDOG_STALL,
        ek.WATCHDOG_RECOVERY,
        ek.AUTOPILOT_DISPATCH,
    ):
        assert not ek.is_pushable(ek.handled_by_for_kind(kind))


def test_orchestrator_and_user_kinds_are_pushable():
    for kind in (
        ek.AGENT_COMPLETE,
        ek.AGENT_FAILURE,
        ek.AGENT_RECOVERY_DISPATCHED,
        ek.TASK_STATUS,
        ek.TOPIC_STATUS,
        ek.MANUAL,
        ek.ORCHESTRATOR_VIOLATION,
    ):
        assert ek.is_pushable(ek.handled_by_for_kind(kind))


def test_agent_complete_is_user_kind_pushable_to_phone():
    """Plan DA log flags this assumption — pin it so a regression is loud."""
    assert ek.handled_by_for_kind(ek.AGENT_COMPLETE) == "user"
    assert ek.is_pushable("user")


def test_unknown_kind_falls_back_to_legacy_routing():
    assert ek.handled_by_for_kind("some_future_kind") == ek.handled_by_for_kind(ek.LEGACY)
