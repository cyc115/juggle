"""T3 — signature-keyed retry caps + novelty gates.

Pins (docs/2026-07-03-integrate-recovery-loop-spec.md §4,
plan/2026-07-03-integrate-recovery-loop.md T3):
  (i)   BL×4 incident (2026-07-02) — identical state re-runs refused.
  (ii)  new signature after a repair = fresh attempt.
  (iii) flapping signatures halt at the 3-total backstop.
  (iv)  trunk-moved blind retry consumes no attempt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from juggle_integrate_envelope import (
    CONFLICT,
    FailContext,
    check_retry_policy,
    compute_signature,
    record_refusal,
    register_attempt,
)


# ── compute_signature() ─────────────────────────────────────────────────────

def test_signature_stable_regardless_of_file_order():
    assert compute_signature("conflict", ["b.py", "a.py"]) == compute_signature(
        "conflict", ["a.py", "b.py"]
    )


def test_signature_differs_for_different_files():
    assert compute_signature("conflict", ["a.py"]) != compute_signature("conflict", ["b.py"])


def test_signature_differs_for_different_class():
    assert compute_signature("conflict", ["a.py"]) != compute_signature("red-suite", ["a.py"])


# ── register_attempt() ──────────────────────────────────────────────────────

def test_register_attempt_increments_counters():
    envelope = {}
    register_attempt(envelope, "sig-a")
    register_attempt(envelope, "sig-a")
    register_attempt(envelope, "sig-b")
    assert envelope["attempts_by_signature"] == {"sig-a": 2, "sig-b": 1}
    assert envelope["attempts_total"] == 3


# ── check_retry_policy() — the four pins ────────────────────────────────────

def test_pin_i_bl_x4_identical_state_rerun_refused():
    """Incident 2026-07-02: blind re-runs against an unmoved trunk/head
    hammered the same failure 4 times. Same signature, trunk/head unmoved
    after the cap (1/signature) is already spent -> refused."""
    prior = {}
    sig = compute_signature(CONFLICT, ["a.py"])
    decision = check_retry_policy(
        prior, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision.allowed is True
    register_attempt(prior, sig)  # simulate the first repair attempt landing

    decision2 = check_retry_policy(
        prior, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision2.allowed is False
    assert "cap" in decision2.reason


def test_pin_ii_new_signature_after_repair_is_fresh_attempt():
    prior = {}
    sig1 = compute_signature(CONFLICT, ["a.py"])
    register_attempt(prior, sig1)  # cap for sig1 now spent

    sig2 = compute_signature(CONFLICT, ["c.py"])  # different conflicting files
    decision = check_retry_policy(
        prior, sig2,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision.allowed is True
    assert decision.consumes_attempt is True


def test_pin_iii_flapping_signatures_halt_at_three_total():
    prior = {}
    sigs = [compute_signature(CONFLICT, [f"f{i}.py"]) for i in range(4)]

    for sig in sigs[:3]:
        decision = check_retry_policy(
            prior, sig,
            trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
            current_trunk="main@aaa", current_head="branch@bbb",
        )
        assert decision.allowed is True
        register_attempt(prior, sig)

    # A 4th, brand-new signature: cap-per-signature would allow it, but the
    # 3-total backstop must still refuse it regardless of signature novelty.
    decision4 = check_retry_policy(
        prior, sigs[3],
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision4.allowed is False
    assert "backstop" in decision4.reason
    assert prior["attempts_total"] == 3


def test_pin_iv_trunk_moved_blind_retry_consumes_no_attempt():
    prior = {}
    sig = compute_signature(CONFLICT, ["a.py"])
    register_attempt(prior, sig)  # cap for sig now spent

    decision = check_retry_policy(
        prior, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@ccc",  # trunk advanced
        current_head="branch@bbb",
    )
    assert decision.allowed is True
    assert decision.consumes_attempt is False


def test_backstop_takes_priority_over_trunk_moved():
    """Even a free (trunk-moved) blind retry must not bypass the 3-total
    backstop — flapping signatures must not retry forever."""
    prior = {"attempts_total": 3, "attempts_by_signature": {}}
    sig = compute_signature(CONFLICT, ["z.py"])
    decision = check_retry_policy(
        prior, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@ccc", current_head="branch@bbb",
    )
    assert decision.allowed is False
    assert "backstop" in decision.reason


# ── record_refusal() integration — handled_by overridden on breach ──────────

@pytest.fixture
def juggle_db(tmp_path):
    from juggle_db import JuggleDB

    d = JuggleDB(db_path=str(tmp_path / "juggle-retry-fixture.db"))
    d.init_db()
    return d


def test_record_refusal_dispatchable_repair_stays_integrate_failed_watchdog(juggle_db):
    from dbops import db_graph

    db_graph.create_task(juggle_db, task_id="t-retry-1", project_id="p1", title="t", prompt="p")
    ctx = FailContext(
        db=juggle_db, thread_id="thread-1", worktree_branch="cyc_X",
        task={"id": "t-retry-1"}, files=["conflict.py"],
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
    )
    record = record_refusal("rebase_conflict", "Rebase conflict on cyc_X onto main.", ctx)

    stored = db_graph.get_task(juggle_db, "t-retry-1")
    envelope = json.loads(stored["fail_envelope"])
    assert envelope["attempts_total"] == 1
    assert record.envelope["signature"] in envelope["attempts_by_signature"]

    notifs = juggle_db.get_notifications_for_session("")
    row = next(n for n in notifs if n["thread_id"] == "thread-1")
    with juggle_db._connect() as conn:
        kind, handled_by = conn.execute(
            "SELECT kind, handled_by FROM notifications_v2 WHERE id = ?", (row["id"],)
        ).fetchone()
    assert kind == "integrate_failed"
    assert handled_by == "watchdog"


def test_record_refusal_cap_breach_escalates_to_repair_exhausted_orchestrator(juggle_db):
    """Pin (i), end-to-end through record_refusal: a second refusal with the
    identical signature and unmoved trunk/head must escalate, not dispatch
    another blind repair (the BL×4 incident)."""
    from dbops import db_graph

    db_graph.create_task(juggle_db, task_id="t-retry-2", project_id="p1", title="t", prompt="p")

    ctx1 = FailContext(
        db=juggle_db, thread_id="thread-2", worktree_branch="cyc_Y",
        task={"id": "t-retry-2"}, files=["conflict.py"],
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
    )
    record_refusal("rebase_conflict", "Rebase conflict on cyc_Y onto main.", ctx1)

    # Re-fetch the task so the second refusal sees the persisted envelope —
    # the same round trip juggle_cmd_integrate._fail() does via _graph_task_for_thread.
    ctx2 = FailContext(
        db=juggle_db, thread_id="thread-2", worktree_branch="cyc_Y",
        task=db_graph.get_task(juggle_db, "t-retry-2"), files=["conflict.py"],
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",  # unmoved
    )
    record2 = record_refusal("rebase_conflict", "Rebase conflict on cyc_Y onto main.", ctx2)

    stored = db_graph.get_task(juggle_db, "t-retry-2")
    envelope = json.loads(stored["fail_envelope"])
    assert envelope["attempts_total"] == 1  # the refused attempt was NOT counted again

    notifs = juggle_db.get_notifications_for_session("")  # newest-first
    rows = sorted((n for n in notifs if n["thread_id"] == "thread-2"), key=lambda n: n["id"])
    with juggle_db._connect() as conn:
        kinds = [
            tuple(conn.execute(
                "SELECT kind, handled_by FROM notifications_v2 WHERE id = ?", (r["id"],)
            ).fetchone())
            for r in rows
        ]
    assert kinds[0] == ("integrate_failed", "watchdog")
    assert kinds[1] == ("repair_exhausted", "orchestrator")
    assert record2.fail_class == CONFLICT
