"""Tests for juggle_integrate_envelope (irl-envelope R2/T2).

classify(): one test per class (spec §2's 5 classes, mechanical from the
failing STEP). handled_by_for(): machinery ALWAYS machinery_error/
orchestrator; every other class is integrate_failed/watchdog. record_refusal()
persists nodes.fail_envelope on the bound graph task and emits the routed
event, with the pinned prose action-item message unchanged (R2).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from juggle_integrate_envelope import (
    COLLISION,
    CONFLICT,
    DIVERGENCE,
    MACHINERY,
    RED_SUITE,
    STEP_DIRTY_WORKTREE,
    STEP_EMPTY_BRANCH,
    STEP_NO_MAIN_BRANCH,
    STEP_REBASE_CONFLICT,
    STEP_SUBMIT_FAILED,
    STEP_TEST_FAILURE,
    STEP_UNEXPECTED,
    FailContext,
    build_envelope,
    classify,
    handled_by_for,
    record_refusal,
)


# ── classify(): one test per class ───────────────────────────────────────────

def test_classify_dirty_worktree_is_collision():
    assert classify(STEP_DIRTY_WORKTREE) == COLLISION


def test_classify_rebase_conflict_is_conflict():
    assert classify(STEP_REBASE_CONFLICT) == CONFLICT


def test_classify_test_failure_is_red_suite():
    assert classify(STEP_TEST_FAILURE) == RED_SUITE


def test_classify_submit_failed_is_divergence():
    assert classify(STEP_SUBMIT_FAILED) == DIVERGENCE


@pytest.mark.parametrize("step", [STEP_NO_MAIN_BRANCH, STEP_EMPTY_BRANCH, STEP_UNEXPECTED])
def test_classify_machinery_steps(step):
    assert classify(step) == MACHINERY


def test_classify_ignores_detail_text_mechanical_from_step_only():
    """Classification is mechanical from the failing step (spec §2) — the
    prose detail must never change the outcome."""
    assert classify(STEP_REBASE_CONFLICT, "mentions machinery and exceptions") == CONFLICT


# ── handled_by_for() routing ─────────────────────────────────────────────────

def test_handled_by_machinery_is_always_machinery_error_orchestrator():
    kind, handled_by = handled_by_for(MACHINERY)
    assert (kind, handled_by) == ("machinery_error", "orchestrator")


@pytest.mark.parametrize("fail_class", [CONFLICT, RED_SUITE, DIVERGENCE, COLLISION])
def test_handled_by_routine_classes_are_integrate_failed_watchdog(fail_class):
    kind, handled_by = handled_by_for(fail_class)
    assert (kind, handled_by) == ("integrate_failed", "watchdog")


def test_build_envelope_shape():
    ctx = FailContext(
        db=None, thread_id="t-1", worktree_branch="cyc_X", worktree_path="/wt",
        files=["a.py"], log_tail="boom", trunk_at_attempt="sha1", head_at_attempt="sha2",
    )
    envelope = build_envelope(CONFLICT, ctx)
    assert envelope == {
        "class": CONFLICT,
        "files": ["a.py"],
        "log_tail": "boom",
        "branch": "cyc_X",
        "worktree": "/wt",
        "attempt": 0,
        "trunk_at_attempt": "sha1",
        "head_at_attempt": "sha2",
    }


# ── record_refusal(): persists envelope + emits event, message text pinned ──

@pytest.fixture
def juggle_db(tmp_path):
    from juggle_db import JuggleDB

    d = JuggleDB(db_path=str(tmp_path / "juggle-fixture.db"))
    d.init_db()
    return d


def test_record_refusal_pinned_action_item_message_text_unchanged(juggle_db):
    """R2: the prose action-item message is byte-identical to the pre-extraction
    inline f-string — zero behavior change."""
    ctx = FailContext(db=juggle_db, thread_id="t-1", worktree_branch="cyc_X")
    record = record_refusal(STEP_REBASE_CONFLICT, "Rebase conflict on cyc_X onto main.", ctx)
    assert record.message == "⚠️ integrate failed [cyc_X]: Rebase conflict on cyc_X onto main."
    items = juggle_db.get_open_action_items()
    assert any(i["message"] == record.message for i in items)


def test_record_refusal_persists_fail_envelope_on_bound_task(juggle_db):
    from dbops import db_graph

    db_graph.create_task(
        juggle_db, task_id="t-envelope", project_id="p1",
        title="task", prompt="do the thing",
    )
    ctx = FailContext(
        db=juggle_db, thread_id="thread-1", worktree_branch="cyc_X",
        task={"id": "t-envelope", "verify_retries": 0}, files=["conflict.py"],
    )

    record = record_refusal(STEP_REBASE_CONFLICT, "Rebase conflict on cyc_X onto main.", ctx)

    assert record.fail_class == CONFLICT
    stored = db_graph.get_task(juggle_db, "t-envelope")
    assert json.loads(stored["fail_envelope"]) == record.envelope


def test_record_refusal_emits_machinery_error_event_for_machinery_class(juggle_db):
    ctx = FailContext(db=juggle_db, thread_id="thread-2", worktree_branch="cyc_Y")
    record = record_refusal(STEP_UNEXPECTED, "Unexpected error during integrate: boom", ctx)

    assert record.fail_class == MACHINERY
    notifs = juggle_db.get_notifications_for_session("")
    assert any(n["thread_id"] == "thread-2" for n in notifs)


def test_record_refusal_no_task_skips_fail_envelope_write_but_still_emits(juggle_db):
    # task=None (non-graph thread) — must not raise, still records + emits.
    ctx = FailContext(db=juggle_db, thread_id="thread-3", worktree_branch="cyc_Z")
    record = record_refusal(
        STEP_DIRTY_WORKTREE,
        "integrate refused: uncommitted changes in /wt (1 file(s)).",
        ctx,
    )
    assert record.fail_class == COLLISION
