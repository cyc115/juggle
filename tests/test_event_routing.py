"""Tests for irl-backbone T1a: event kinds enum + delivery routing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops import event_kinds as ek


def test_twelve_kinds_defined():
    assert len(ek.ALL_KINDS) == 12


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
