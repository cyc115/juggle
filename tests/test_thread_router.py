"""Tests for juggle_thread_router — the SYNC-LOCAL in-hook thread router.

Pure-function scorer/guard tests need no DB (spec Q4). DB-backed router tests
live alongside in the same file using JuggleDB(tmp_path).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB
from juggle_thread_router import (
    SWITCH_THRESHOLD,
    _is_trivial_followup,
    _route_prompt_to_thread,
    _score,
)


# ---------------------------------------------------------------------------
# _is_trivial_followup — highest-priority guard
# ---------------------------------------------------------------------------


def test_trivial_followup_short_prompts():
    for prompt in ["try again", "status", "status?", "retry", "continue", "go",
                   "yes", "no", "ok", "thanks", "?"]:
        assert _is_trivial_followup(prompt), f"expected trivial: {prompt!r}"


def test_trivial_followup_empty_prompt():
    assert _is_trivial_followup("")
    assert _is_trivial_followup("   ")


def test_trivial_followup_long_prompt_is_not_trivial():
    assert not _is_trivial_followup(
        "please refactor the payment gateway retry logic to use exponential backoff"
    )


# ---------------------------------------------------------------------------
# _score — pure scorer, no DB
# ---------------------------------------------------------------------------


def test_score_high_overlap_scores_above_switch_threshold():
    thread = {"title": "Refactor payment gateway retries", "user_label": "P"}
    score = _score("please refactor the payment gateway retries logic", thread)
    assert score >= SWITCH_THRESHOLD


def test_score_no_overlap_scores_low():
    thread = {"title": "Refactor payment gateway retries", "user_label": "P"}
    score = _score("what's the weather like today", thread)
    assert score < SWITCH_THRESHOLD


def test_score_uses_recent_snippet():
    thread = {"title": "Misc", "user_label": "Q", "_snippet": "payment gateway retries"}
    score = _score("payment gateway retries need a fix", thread)
    assert score >= SWITCH_THRESHOLD


def test_score_recency_bonus_breaks_ties():
    thread = {"title": "Misc", "user_label": "Q"}
    low = _score("zzz", thread, recency_bonus=0.0)
    high = _score("zzz", thread, recency_bonus=0.05)
    assert high > low


# ---------------------------------------------------------------------------
# _route_prompt_to_thread — DB-backed integration of the gates above
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    return db


def test_route_trivial_followup_stays_put(tmp_path):
    db = _make_db(tmp_path)
    current = db.create_thread("Topic A", session_id="s1")
    db.create_thread("Refactor payment gateway retries", session_id="s1")
    db.set_current_thread(current)

    routed = _route_prompt_to_thread(db, "status?", current)
    assert routed == current


def test_route_confident_match_switches(tmp_path):
    db = _make_db(tmp_path)
    current = db.create_thread("Topic A", session_id="s1")
    other = db.create_thread("Refactor payment gateway retries", session_id="s1")
    db.set_current_thread(current)

    routed = _route_prompt_to_thread(
        db, "please refactor the payment gateway retries logic", current
    )
    assert routed == other


def test_route_ambiguous_match_stays_put(tmp_path):
    db = _make_db(tmp_path)
    current = db.create_thread("Topic A", session_id="s1")
    db.create_thread("Refactor payment gateway retries", session_id="s1")
    db.set_current_thread(current)

    routed = _route_prompt_to_thread(db, "what's the weather like today", current)
    assert routed == current


def test_route_no_candidates_stays_put(tmp_path):
    db = _make_db(tmp_path)
    current = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(current)

    routed = _route_prompt_to_thread(db, "please refactor the payment gateway", current)
    assert routed == current
