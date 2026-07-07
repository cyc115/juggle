"""Unit pins for dbops.terminal_states.topic_work_landed — the single
"agent's bound work has landed" predicate shared by the watchdog completed-agent
reaper, the stalled re-dispatch guard, and release-reconcile.

2026-07-07 completed agents leak / watchdog re-dispatches merged topic.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dbops.terminal_states import topic_work_landed


def test_none_topic_is_not_landed():
    assert topic_work_landed(None) is False


def test_verified_and_merged_is_landed():
    assert topic_work_landed({"state": "verified", "merged_sha": "deadbeef"}) is True


def test_merged_sha_alone_is_landed():
    """A merged_sha in any non-verified state (integrated-unlanded / done) still
    means the work landed."""
    assert topic_work_landed({"state": "integrated-unlanded", "merged_sha": "c0ffee"}) is True
    assert topic_work_landed({"state": "done", "merged_sha": "c0ffee"}) is True


def test_landed_terminal_state_without_merged_sha_is_landed():
    for state in ("verified", "delivered", "done", "integrated-unlanded"):
        assert topic_work_landed({"state": state, "merged_sha": None}) is True, state


def test_active_topic_is_not_landed():
    assert topic_work_landed({"state": "running", "merged_sha": None}) is False
    assert topic_work_landed({"state": "ready", "merged_sha": None}) is False
    assert topic_work_landed({"state": "integrating", "merged_sha": None}) is False


def test_failure_terminal_is_not_landed():
    """A failed topic may be legitimately re-dispatched — never 'landed'."""
    assert topic_work_landed({"state": "failed-exec", "merged_sha": None}) is False
