"""Tests for irl-backbone T1a: event kinds enum + delivery routing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops import event_kinds as ek


def test_sixteen_kinds_defined():
    """irl-envelope T2 adds integrate_failed + machinery_error to T1a's 12;
    irl-retry T3 adds repair_exhausted; irl-repair T4 adds repair_dispatched."""
    assert len(ek.ALL_KINDS) == 16


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
