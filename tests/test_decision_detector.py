"""Pending-decision detector + auto-file (2026-06-30 action-item reliability).

RCA 2026-06-30: user-facing decisions the orchestrator raises in PROSE
('parked for your go', 'want me to?', 'needs a call') never reached the cockpit
Action Items pane because the prose detector's cue set was too narrow. The pure
detect_pending_decision broadens it; the Stop seam auto-files a deduped decision
item so users never lose track of what they must act on.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_hooks_prose import detect_pending_decision, record_prose_decision


# --- truth table: decision/blocker/permission prose -> True ------------------
@pytest.mark.parametrize("text", [
    "Want me to wire it into the dispatch path?",
    "Parked for your go — say the word and I'll ship it.",
    "This one is your call.",
    "Should I proceed with option A?",
    "Ready to green-light the migration?",
    "Awaiting your go before I merge.",
    "This needs a call from you.",
    "That needs a decision before I continue.",
    "Which option do you prefer?",
    "Do you want me to revert?",
    "Let me know how to proceed.",
])
def test_detects_decision_prose(text):
    assert detect_pending_decision(text) is True


# --- truth table: plain status / FYI -> False --------------------------------
@pytest.mark.parametrize("text", [
    "Done. Tests green.",
    "Implemented and verified; pushed to main.",
    "The build passed and coverage held.",
    "Here is a summary of the changes I made.",
    "Fixed the bug and added a regression pin.",
])
def test_ignores_plain_status(text):
    assert detect_pending_decision(text) is False


# --- auto-file on a decision turn, deduped on repeat -------------------------
def test_decision_turn_auto_files_deduped(juggle_db):
    """2026-06-30 action-item reliability: a decision-turn with no filed item
    auto-creates ONE decision action item; re-detection across turns does not spam."""
    tid = juggle_db.create_thread(topic="Feature X", session_id="s")
    juggle_db.set_current_thread(tid)
    msg = "Want me to green-light the schema migration? It's your call."

    record_prose_decision(juggle_db, msg)
    record_prose_decision(juggle_db, msg)  # repeat turn — must dedup

    decisions = [i for i in juggle_db.get_open_action_items()
                 if i.get("message", "").startswith("[auto-decision]")]
    assert len(decisions) == 1
    assert decisions[0]["type"] == "decision"


def test_no_decision_files_nothing(juggle_db):
    """2026-06-30 action-item reliability: a clean status turn files no item."""
    tid = juggle_db.create_thread(topic="Feature Y", session_id="s")
    juggle_db.set_current_thread(tid)
    record_prose_decision(juggle_db, "Done. All tests green.")
    assert juggle_db.get_open_action_items() == []
