"""Tests for the watchdog stalled-pane detector (juggle_watchdog_stall).

Feature (user-approved 2026-07-01, Option B): coder agents finish work, print a
recap, and idle at the ready prompt without finalizing. The detector nudges a
busy agent sitting at the harness READY prompt with NO activity indicator,
debounced across ticks, then escalates to a HIGH action item after a bounded
number of nudges.

Detection uses the harness adapter's readiness/submission markers as the SSOT —
no hardcoded Claude strings here or in the detector.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# claude-harness markers (SSOT — mirror juggle_harness_defaults, used as fixtures)
READY = ("shift+tab to cycle", "bypass permissions on", "/effort")
SUBMIT = ("esc to interrupt", "✻", "✶")


# ── pure detector: is_idle_at_prompt ─────────────────────────────────────────


def test_idle_at_prompt_true_when_ready_and_no_submission():
    from juggle_watchdog_stall import is_idle_at_prompt

    pane = "recap: done.\n\n > \n  shift+tab to cycle"
    assert is_idle_at_prompt(pane, READY, SUBMIT) is True


def test_idle_at_prompt_false_when_submission_marker_present():
    """Active/thinking pane (submission marker visible) is never a stall."""
    from juggle_watchdog_stall import is_idle_at_prompt

    pane = "Running tests…\n✻ Cooking (26s · esc to interrupt)\n  shift+tab to cycle"
    assert is_idle_at_prompt(pane, READY, SUBMIT) is False


def test_idle_at_prompt_false_when_no_readiness_marker():
    from juggle_watchdog_stall import is_idle_at_prompt

    assert is_idle_at_prompt("some shell output\n$ ", READY, SUBMIT) is False
    assert is_idle_at_prompt(None, READY, SUBMIT) is False
    assert is_idle_at_prompt("", READY, SUBMIT) is False


# ── state machine: StallTracker.decide ───────────────────────────────────────


def _decide(tracker, aid, idle, now, *, threshold_s=180.0, max_nudges=2, dk="d0"):
    return tracker.decide(
        aid, idle=idle, now=now, threshold_s=threshold_s,
        max_nudges=max_nudges, dispatch_key=dk,
    )


def test_no_nudge_before_threshold():
    """(a) idle detected only after >= threshold across consecutive ticks."""
    from juggle_watchdog_stall import StallTracker

    t = StallTracker()
    assert _decide(t, "A", True, 0.0) == "waiting"      # first idle → start clock
    assert _decide(t, "A", True, 60.0) == "waiting"     # 60s < 180s
    assert _decide(t, "A", True, 179.0) == "waiting"    # still under threshold
    assert _decide(t, "A", True, 180.0) == "nudge"      # threshold reached


def test_no_false_positive_on_active_pane():
    """An actively-working pane (idle=False) never nudges, even across ticks."""
    from juggle_watchdog_stall import StallTracker

    t = StallTracker()
    for now in (0.0, 200.0, 400.0, 600.0):
        assert _decide(t, "A", False, now) == "active"


def test_nudges_up_to_max_then_escalates_then_silent():
    """(b) at most max_stall_nudges nudges, then one escalation, then silence."""
    from juggle_watchdog_stall import StallTracker

    t = StallTracker()
    # start clock
    assert _decide(t, "A", True, 0.0) == "waiting"
    # nudge #1 at threshold; each nudge re-arms the debounce clock
    assert _decide(t, "A", True, 180.0) == "nudge"
    assert t.nudges["A"] == 1
    assert _decide(t, "A", True, 300.0) == "waiting"    # within re-armed window
    # nudge #2 after another threshold
    assert _decide(t, "A", True, 360.0) == "nudge"
    assert t.nudges["A"] == 2
    assert _decide(t, "A", True, 480.0) == "waiting"    # within re-armed window
    # escalate one threshold after the last nudge (max reached)
    assert _decide(t, "A", True, 540.0) == "escalate"
    # thereafter silent — no repeat escalation, no more nudges
    assert _decide(t, "A", True, 720.0) == "silent"
    assert _decide(t, "A", True, 2000.0) == "silent"


def test_counter_resets_on_new_dispatch():
    """(c) nudge counter + escalation reset when the dispatch identity changes."""
    from juggle_watchdog_stall import StallTracker

    t = StallTracker()
    _decide(t, "A", True, 0.0, dk="d1")
    assert _decide(t, "A", True, 180.0, dk="d1") == "nudge"
    assert t.nudges["A"] == 1
    # New dispatch (agent reused / re-dispatched) → fresh state
    assert _decide(t, "A", True, 190.0, dk="d2") == "waiting"
    assert t.nudges.get("A", 0) == 0
    assert _decide(t, "A", True, 370.0, dk="d2") == "nudge"
    assert t.nudges["A"] == 1


def test_activity_clears_idle_clock():
    """Any activity clears first-seen-idle so the debounce restarts from scratch."""
    from juggle_watchdog_stall import StallTracker

    t = StallTracker()
    assert _decide(t, "A", True, 0.0) == "waiting"
    assert _decide(t, "A", False, 100.0) == "active"    # agent resumed
    assert _decide(t, "A", True, 150.0) == "waiting"    # clock restarts here
    assert _decide(t, "A", True, 250.0) == "waiting"    # 100s since restart < 180s
    assert _decide(t, "A", True, 330.0) == "nudge"      # 180s since restart


# ── driver: check_stalled_agents (IO glue) ───────────────────────────────────


def _mk_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    return db


def _busy_agent(db, thread_id, pane_id="%1", last_task="do the thing"):
    """Create a busy interactive coder agent bound to a thread; return its id."""
    aid = db.create_agent(role="coder", pane_id=pane_id, harness="claude")
    db.update_agent(aid, status="busy", assigned_thread=thread_id,
                    last_task=last_task, busy_since="2026-07-01T00:00:00Z")
    return aid


def test_driver_nudges_idle_agent_via_send_message(tmp_path, monkeypatch):
    import juggle_watchdog_stall as stall

    db = _mk_db(tmp_path)
    # Register a busy, interactive coder agent bound to a thread.
    th = db.create_thread("stall-topic", session_id="s0")
    _busy_agent(db, th, pane_id="%1")

    mgr = MagicMock()
    tracker = stall.StallTracker()
    # Inject canned pane + markers so no tmux/config is needed.
    idle_pane = "recap done\n > \nshift+tab to cycle"
    caps = {"%1": idle_pane}

    def capture(pane_id):
        return caps.get(pane_id)

    def markers_for(agent):
        return READY, SUBMIT

    # tick 1: first idle → pending (no send)
    stall.check_stalled_agents(db, mgr, tracker, now=0.0, session_id="s0",
                               capture=capture, markers_for=markers_for)
    mgr.send_message.assert_not_called()

    # tick 2: threshold reached → one nudge with the finalize instruction
    stall.check_stalled_agents(db, mgr, tracker, now=180.0, session_id="s0",
                               capture=capture, markers_for=markers_for)
    assert mgr.send_message.call_count == 1
    sent_text = mgr.send_message.call_args[0][1]
    assert "finalize" in sent_text.lower()
    assert "blocker" in sent_text.lower()


def test_driver_does_not_nudge_active_agent(tmp_path):
    import juggle_watchdog_stall as stall

    db = _mk_db(tmp_path)
    th = db.create_thread("active-topic", session_id="s0")
    _busy_agent(db, th, pane_id="%1", last_task="do it")

    mgr = MagicMock()
    tracker = stall.StallTracker()
    active_pane = "✻ Cooking (26s · esc to interrupt)\nshift+tab to cycle"

    def capture(pane_id):
        return active_pane

    def markers_for(agent):
        return READY, SUBMIT

    for now in (0.0, 180.0, 360.0, 900.0):
        stall.check_stalled_agents(db, mgr, tracker, now=now, session_id="s0",
                                   capture=capture, markers_for=markers_for)
    mgr.send_message.assert_not_called()


def test_driver_files_high_action_item_after_max_nudges(tmp_path):
    import juggle_watchdog_stall as stall

    db = _mk_db(tmp_path)
    th = db.create_thread("escalate-topic", session_id="s0")
    _busy_agent(db, th, pane_id="%1", last_task="finish")

    mgr = MagicMock()
    tracker = stall.StallTracker()

    def capture(pane_id):
        return "idle recap\n > \nshift+tab to cycle"

    def markers_for(agent):
        return READY, SUBMIT

    # Drive well past 2 nudges + escalation (threshold 180s default).
    now = 0.0
    for _ in range(12):
        stall.check_stalled_agents(db, mgr, tracker, now=now, session_id="s0",
                                   capture=capture, markers_for=markers_for)
        now += 180.0

    # Exactly max_stall_nudges (2) nudges were sent…
    assert mgr.send_message.call_count == 2
    # …and a HIGH action item was filed for the thread.
    items = [i for i in db.get_open_action_items()
             if i.get("thread_id") == th and i.get("priority") == "high"]
    assert len(items) == 1, items
    assert "prompt" in items[0]["message"].lower()
