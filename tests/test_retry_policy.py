"""T3 — signature-keyed retry caps + novelty gates.

Pins (docs/2026-07-03-integrate-recovery-loop-spec.md §4,
plan/2026-07-03-integrate-recovery-loop.md T3):
  (i)   BL×4 incident (2026-07-02) — identical state re-runs refused.
  (ii)  new signature after a repair = fresh attempt.
  (iii) flapping signatures halt at the 3-total backstop.
  (iv)  trunk-moved blind retry consumes no attempt.
"""

from __future__ import annotations

from juggle_integrate_envelope import (
    check_retry_policy,
    classify,
    compute_signature,
    emit_repair_exhausted,
    register_attempt,
)


# ── classify() ──────────────────────────────────────────────────────────────

def test_classify_rebase_conflict():
    assert classify("update_to", "Rebase conflict on foo onto main") == "conflict"


def test_classify_test_failure():
    assert classify("run_test_cmd_full", "2 failed, 10 passed") == "red-suite"


def test_classify_fast_forward_refusal():
    assert classify("submit", "fast-forward refused, diverged from main") == "divergence"


def test_classify_dirty_worktree():
    assert classify("dirty_files", "uncommitted changes / untracked files") == "collision"


def test_classify_unrecognized_is_machinery():
    assert classify("submit", "TypeError: NoneType has no attribute 'foo'") == "machinery"


# ── compute_signature() ─────────────────────────────────────────────────────

def test_signature_stable_for_same_class_and_files():
    sig1 = compute_signature("conflict", ["b.py", "a.py"])
    sig2 = compute_signature("conflict", ["a.py", "b.py"])
    assert sig1 == sig2


def test_signature_differs_for_different_files():
    sig1 = compute_signature("conflict", ["a.py"])
    sig2 = compute_signature("conflict", ["b.py"])
    assert sig1 != sig2


def test_signature_differs_for_different_class():
    sig1 = compute_signature("conflict", ["a.py"])
    sig2 = compute_signature("red-suite", ["a.py"])
    assert sig1 != sig2


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
    """Incident 2026-07-02: blind re-runs against an unmoved trunk/head hammered
    the same failure 4 times. Same signature, trunk/head unmoved after the cap
    (1/signature) is already spent → refused."""
    envelope = {}
    sig = compute_signature("conflict", ["a.py"])
    decision = check_retry_policy(
        envelope, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision.allowed is True
    register_attempt(envelope, sig)

    # Second attempt, same signature, trunk/head STILL unmoved — this is the
    # BL×4 pattern: refuse instead of blindly re-running.
    decision2 = check_retry_policy(
        envelope, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision2.allowed is False
    assert "cap" in decision2.reason


def test_pin_ii_new_signature_after_repair_is_fresh_attempt():
    envelope = {}
    sig1 = compute_signature("conflict", ["a.py"])
    register_attempt(envelope, sig1)  # cap for sig1 now spent

    sig2 = compute_signature("conflict", ["c.py"])  # different conflicting files
    decision = check_retry_policy(
        envelope, sig2,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision.allowed is True
    assert decision.consumes_attempt is True


def test_pin_iii_flapping_signatures_halt_at_three_total():
    envelope = {}
    sigs = [compute_signature("conflict", [f"f{i}.py"]) for i in range(4)]

    for sig in sigs[:3]:
        decision = check_retry_policy(
            envelope, sig,
            trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
            current_trunk="main@aaa", current_head="branch@bbb",
        )
        assert decision.allowed is True
        register_attempt(envelope, sig)

    # A 4th, brand-new signature — cap-per-signature would allow it, but the
    # 3-total backstop must still refuse it regardless of signature novelty.
    decision4 = check_retry_policy(
        envelope, sigs[3],
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@aaa", current_head="branch@bbb",
    )
    assert decision4.allowed is False
    assert "backstop" in decision4.reason
    assert envelope["attempts_total"] == 3


def test_pin_iv_trunk_moved_blind_retry_consumes_no_attempt():
    envelope = {}
    sig = compute_signature("conflict", ["a.py"])
    register_attempt(envelope, sig)  # cap for sig now spent

    decision = check_retry_policy(
        envelope, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@ccc",  # trunk advanced
        current_head="branch@bbb",
    )
    assert decision.allowed is True
    assert decision.consumes_attempt is False


def test_backstop_takes_priority_over_trunk_moved():
    """Even a free (trunk-moved) blind retry must not bypass the 3-total
    backstop — flapping signatures must not retry forever."""
    envelope = {"attempts_total": 3, "attempts_by_signature": {}}
    sig = compute_signature("conflict", ["z.py"])
    decision = check_retry_policy(
        envelope, sig,
        trunk_at_attempt="main@aaa", head_at_attempt="branch@bbb",
        current_trunk="main@ccc", current_head="branch@bbb",
    )
    assert decision.allowed is False
    assert "backstop" in decision.reason


# ── emit_repair_exhausted() ──────────────────────────────────────────────────

def test_emit_repair_exhausted_writes_orchestrator_event():
    from juggle_db import JuggleDB

    db = JuggleDB()
    envelope = {"attempts_total": 3, "attempts_by_signature": {"sig-a": 1, "sig-b": 1, "sig-c": 1}}
    notif_id = emit_repair_exhausted(db, thread_id=None, session_id="", topic_id="T-x", envelope=envelope)

    rows = db.get_notifications_for_session("")
    row = next(r for r in rows if r["id"] == notif_id)
    assert "repair_exhausted" not in row  # kind isn't in the SELECT surface, message is
    assert "T-x" in row["message"]

    with db._connect() as conn:
        kind, handled_by = conn.execute(
            "SELECT kind, handled_by FROM notifications_v2 WHERE id = ?", (notif_id,)
        ).fetchone()
    assert kind == "repair_exhausted"
    assert handled_by == "orchestrator"
