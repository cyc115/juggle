"""Scroll-stable paste-submit detection — 2026-07-06 paste-submit race (#3).

Incident (recurring, high-value): `agent send-task` reports "submission not
verified for pane %N — task not sent", but the task text IS pasted in the pane;
the agent then sits idle-looking (Enter swallowed) until the ~36-min stall
detector catches it.

ROOT CAUSE (this pin): submission verification keyed on statusline markers that
a background notification can scroll out of the fixed capture tail:
  * `input_box_stuck` gated its STRUCTURAL "box still holds text" detection on a
    readiness marker ("shift+tab to cycle") being visible (juggle_paste_submit
    :67). When a trailing notification scrolls that marker off, a box STILL
    holding the pasted text is not classified stuck → the C-m retry never fires
    → false "not sent" while the text sits in the box.
  * the queued-messages footer was an UNCONDITIONAL success (juggle_tmux :298),
    so a residual paste in the box alongside a stale pre-existing queued message
    was scored submitted → the swallowed Enter was never re-sent.

The fix keys on the SCROLL-STABLE input-box frame (bottom-pinned `│ ❯ … │`),
not on the statusline marker. This module pins the pure classifier
`classify_pane_submission(capture) -> 'submitted'|'stuck'|'unknown'` — the
agent-verifiable seam that needs no live tmux race.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_paste_submit import classify_pane_submission  # noqa: E402
from juggle_spawn_readiness import (  # noqa: E402
    _ACTIVE_STATUS_PATTERN,
    _ACTIVITY_MARKERS,
    _READY_MARKERS,
    _SUBMISSION_MARKERS,
)


def _classify(bottom, head=""):
    return classify_pane_submission(
        bottom,
        head,
        _READY_MARKERS,
        _SUBMISSION_MARKERS,
        _ACTIVITY_MARKERS,
        _ACTIVE_STATUS_PATTERN,
    )


# ── (a) markers scrolled off, box still holds the paste → STUCK (re-send Enter) ─


def test_box_content_without_ready_marker_is_stuck():
    """2026-07-06 paste-submit race: notification scrolls statusline markers →
    false 'not sent'.

    A drawn input box STILL holding the (unenumerated) collapsed-paste text, with
    NO readiness marker in the captured tail (a trailing notification scrolled the
    'shift+tab to cycle' footer off). Pre-fix: structural stuck detection is gated
    on the readiness marker, so this is NOT classified stuck → no Enter retry →
    the silent false 'not sent'. Must classify as 'stuck' so the C-m retry fires.
    """
    capture = (
        "  ⏺ wrote src/foo.py\n"
        "╭────────────────────────────────────────────────╮\n"
        "│ ❯ paste again to expand                        │\n"
        "╰────────────────────────────────────────────────╯\n"
        "  ✱ cockpit: agent XY completed — 3 topics active\n"
        "    Wrote reports/dogfood-2026-07-06.md\n"
    )
    assert _classify(capture) == "stuck"


# ── (b) queued footer + residual box content → STUCK, not a false success ──────


def test_queued_footer_with_residual_box_content_is_stuck():
    """Queued-messages footer must NOT mask an unsubmitted paste.

    'Press up to edit queued messages' only means a queue is non-empty (a stale
    pre-existing queued message). If the input box STILL holds the just-pasted
    text (Enter swallowed), the paste is unsubmitted. Pre-fix: the footer is an
    unconditional success → False positive, Enter never re-sent. Must be 'stuck'.
    """
    capture = (
        "  Agent is processing…\n"
        "╭────────────────────────────────────────────────╮\n"
        "│ ❯ paste again to expand                        │\n"
        "╰────────────────────────────────────────────────╯\n"
        "  Press up to edit queued messages\n"
    )
    assert _classify(capture) == "stuck"


# ── scroll-stable SUCCESS: empty drawn box, markers scrolled off → SUBMITTED ───


def test_empty_drawn_box_without_ready_marker_is_submitted():
    """A demonstrably EMPTY drawn input box proves the text left the box even when
    every statusline marker (submission AND readiness) scrolled off behind a
    trailing notification. Pre-fix the empty-box success path REQUIRED a readiness
    marker (juggle_tmux :349), so this was a false 'not sent'. Must be 'submitted'.
    """
    capture = (
        "  Read(src/juggle_tmux.py)\n"
        "╭────────────────────────────────────────────────╮\n"
        "│ ❯                                              │\n"
        "╰────────────────────────────────────────────────╯\n"
        "  ✱ cockpit: agent XY completed — 3 topics active\n"
    )
    assert _classify(capture) == "submitted"


# ── guardrails: don't over-generalize the scroll-stable success ───────────────


def test_no_box_no_marker_is_unknown_not_submitted():
    """A pane with neither a drawn box nor any marker (blank/crashed) is NOT proof
    of submission — must be 'unknown' (falls through to False), never 'submitted'.
    Guards the empty-box success path against 'process alive' style false wins."""
    assert _classify("  idle scrollback only\n") == "unknown"


def test_queued_footer_with_empty_box_is_submitted():
    """The legitimate queued state: footer present AND the box is empty — the
    message was accepted into the queue. Must stay 'submitted' (preserves the
    v1.46.1 send_message queued success)."""
    capture = "Agent is processing…\nPress up to edit queued messages\n> \n"
    assert _classify(capture) == "submitted"


def test_submission_marker_is_submitted():
    """An explicit submission marker is the fast-path success."""
    assert _classify("✻ Working… (esc to interrupt)\n") == "submitted"


# ── wait_for_submission integration pins (RED on pre-fix wait_for_submission) ──


@pytest.fixture
def mgr():
    from juggle_tmux import JuggleTmuxManager

    return JuggleTmuxManager(session_name="juggle-test")


def test_wait_for_submission_retries_enter_when_box_content_but_marker_scrolled(mgr):
    """2026-07-06 paste-submit race: notification scrolls statusline markers →
    false 'not sent'.

    Box still holds the (unenumerated) paste, no readiness marker in the tail
    (scrolled off by a trailing notification). Pre-fix `input_box_stuck` gated on
    the readiness marker → not stuck → the C-m retry NEVER fired and the result
    was a silent False ('task not sent') with the text left in the box. Post-fix:
    the frame-anchored classifier sees the residual box → fires C-m; once the box
    clears to a submission marker, returns True.
    """
    from unittest.mock import MagicMock, patch

    stuck = (
        "  ✱ cockpit: agent XY completed — 3 topics active\n"
        "╭────────────────────────────────────────────────╮\n"
        "│ ❯ paste again to expand                        │\n"
        "╰────────────────────────────────────────────────╯\n"
        "    Wrote reports/dogfood-2026-07-06.md\n"
    )
    marker = "✻ Working… (esc to interrupt)\n"
    outputs = iter([stuck, stuck, marker])
    enter_calls: list = []

    def fake_run(*args):
        if args[0] == "capture-pane":
            return MagicMock(stdout=next(outputs), returncode=0)
        if args[0] == "send-keys":
            enter_calls.append(args)
        return MagicMock(stdout="", returncode=0)

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        result = mgr.wait_for_submission("%9", "implement it", timeout=10, max_enter_retries=5)

    assert result is True
    assert len(enter_calls) >= 1, (
        "residual box with the readiness marker scrolled off MUST fire a C-m retry "
        "(pre-fix: gated on the readiness marker → 0 retries, silent false 'not sent')"
    )
    assert enter_calls[0][-1] == "C-m"


def test_wait_for_submission_false_when_queued_footer_but_box_still_full(mgr):
    """Queued-messages footer must NOT mask a residual, unsubmitted paste.

    Agent mid-turn with a stale pre-existing queued message (footer shown) AND the
    just-pasted text still in the box (Enter swallowed). Pre-fix the footer was an
    unconditional success → True (false positive), Enter never re-sent. Post-fix:
    residual box content wins → C-m retries fire; the box never clears here, so the
    honest verdict is False (a genuine wedge to surface), not a false success.
    """
    from unittest.mock import MagicMock, patch

    queued_and_full = (
        "  Agent is processing…\n"
        "╭────────────────────────────────────────────────╮\n"
        "│ ❯ paste again to expand                        │\n"
        "╰────────────────────────────────────────────────╯\n"
        "  Press up to edit queued messages\n"
    )

    with (
        patch.object(mgr, "_run_tmux", return_value=MagicMock(stdout=queued_and_full, returncode=0)),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%9", "implement it", timeout=3, max_enter_retries=2)

    assert result is False, (
        "a residual paste under a stale queued-messages footer must NOT be scored "
        "submitted (pre-fix: unconditional footer success returned True)"
    )
