"""dbops.db_node_machine — unified node state machine, incl. async-land
unlanded-state transitions (SPEC §5.1): integrating -[integrate_submitted]->
integrated-unlanded -[land_confirmed]-> verified / -[land_fail]-> failed-integration.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops.db_node_machine import (  # noqa: E402
    InvalidTransition,
    legal_events,
    node_transition,
)


def test_integrating_submit_goes_unlanded():
    assert node_transition("integrating", "integrate_submitted", "task") == "integrated-unlanded"


def test_unlanded_land_confirmed_goes_verified():
    assert node_transition("integrated-unlanded", "land_confirmed", "task") == "verified"


def test_unlanded_land_fail_goes_failed_integration():
    assert node_transition("integrated-unlanded", "land_fail", "task") == "failed-integration"


def test_unlanded_can_archive():
    assert node_transition("integrated-unlanded", "archive", "task") == "archived"


def test_integrate_ok_still_direct_to_verified():
    """Regression pin: git-direct (synchronous submit) keeps the pre-existing
    integrating -[integrate_ok]-> verified edge — unlanded-state is additive."""
    assert node_transition("integrating", "integrate_ok", "task") == "verified"


def test_new_events_legal_for_task_kind():
    legal = legal_events("task")
    assert {"integrate_submitted", "land_confirmed", "land_fail"} <= legal


def test_new_events_illegal_for_research_kind():
    """research nodes never integrate through the async-land path."""
    with pytest.raises(InvalidTransition):
        node_transition("integrating", "integrate_submitted", "research")


def test_unknown_event_from_unlanded_raises():
    with pytest.raises(InvalidTransition):
        node_transition("integrated-unlanded", "bogus_event", "task")
