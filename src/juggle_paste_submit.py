"""Paste-without-submit detection & recovery (2026-07-03 DA %6852 incident).

Two cooperating halves, extracted here so juggle_tmux / juggle_watchdog /
juggle_watchdog_daemon stay within the LOC gate and share one source of truth:

  * dispatch-side STRUCTURAL stuck detection (``input_box_stuck`` /
    ``input_box_has_content``) — independent of placeholder WORDING, so a new
    collapsed-paste string ("paste again to expand") still classifies as stuck.
  * watchdog seconds-scale FAST-SWEEP: the predicate (``is_unsubmitted``) plus
    the recovery ladder (``handle_unsubmitted``: resend Enter → Escape+re-paste
    once → fail loud with a DISPATCH_FAILED event routed to the orchestrator).
"""
from __future__ import annotations

import logging

from dbops import event_kinds as _ek

_log = logging.getLogger("juggle-watchdog")

# Seconds since dispatch before an unchanged pane is treated as a wedge (below
# this the TUI render may still be settling). Well under the coarse 60s ``stuck``
# floor so the backstop reacts in seconds, not the ~36-min coarse path.
FAST_SWEEP_FLOOR_SECS = 8.0


# --- dispatch-side structural detection (used by juggle_tmux) --------------


def _bottom_input_box(bottom: str) -> list[str]:
    """Return the interior ``│``-framed lines of the bottom-most input box.

    The Claude input widget is the LAST contiguous run of ``│``-framed lines and
    always carries a ``❯``/``>`` prompt glyph — unlike a stray ``│``-framed table
    in the transcript above. Scanned bottom-up so a footer or a NOTIFICATION
    rendered below/around the box (non-``│`` lines) is skipped. Returns ``[]``
    when no input box is present.

    This anchor is the fix for the 2026-07-06 paste-submit race (#3): the box
    frame is bottom-pinned and cannot scroll away, so it survives a background
    notification that pushes the statusline readiness/submission markers out of
    the captured tail — unlike keying on those markers.
    """
    block: list[str] = []
    for raw in reversed(bottom.splitlines()):
        s = raw.strip()
        if s.startswith("│"):
            block.append(s)
        elif block:
            break
    block.reverse()
    if not block:
        return []
    if not any("❯" in ln or ln.strip("│").strip().startswith(">") for ln in block):
        return []  # a stray │-framed transcript table, not the input widget
    return block


def input_box_present(bottom: str) -> bool:
    """True if a drawn input box (bottom-pinned ``│ ❯ … │`` widget) is visible."""
    return bool(_bottom_input_box(bottom))


def input_box_has_content(bottom: str) -> bool:
    """True if the drawn input box still holds a non-blank line.

    Scans the bottom-most input box's interior lines and returns True if any
    still has visible glyphs after stripping the frame, the ``❯``/``>`` prompt
    glyph, and whitespace. An empty box (``│ ❯                  │``) reads as
    empty. Structural signal, independent of any collapsed-paste wording.
    """
    for s in _bottom_input_box(bottom):
        inner = s.strip("│").strip().lstrip("❯>").strip()
        if inner:
            return True
    return False


def input_box_stuck(bottom: str, head: str, ready_markers) -> bool:
    """True if the input box still holds unsubmitted text.

    Enumerated fast-path hints (collapsed-paste placeholder / INSERT banner /
    prompt head / a non-empty bare ``❯``/``>`` line) PLUS a STRUCTURAL,
    SCROLL-STABLE fallback: the bottom-pinned input-box frame still holds a
    non-blank interior line. Keyed on the box FRAME itself, not on a readiness
    marker — a background notification can scroll the 'shift+tab to cycle' footer
    off the captured tail, but the box frame cannot scroll away (2026-07-06
    paste-submit race #3). ``ready_markers`` is retained for call-site
    compatibility; the frame anchor supersedes it.
    """
    del ready_markers  # frame-anchored detection no longer needs the marker gate
    if (
        "[Pasted text" in bottom
        or "-- INSERT --" in bottom
        or (head and head in bottom)
        or any(
            line.strip().startswith(("❯ ", "> ")) and len(line.strip()) > 2
            for line in bottom.splitlines()
        )
    ):
        return True
    return input_box_has_content(bottom)


def classify_pane_submission(
    bottom: str,
    head: str,
    ready_markers,
    submission_markers,
    activity_markers,
    active_pattern: str,
) -> str:
    """Pure classifier: ``'submitted' | 'stuck' | 'unknown'`` from a pane tail.

    The agent-verifiable seam for paste-submit verification (no live tmux race).
    SCROLL-STABLE contract (2026-07-06 paste-submit race #3): a background
    notification can push the statusline readiness/submission markers out of the
    fixed capture tail, so submission is NOT proven by those markers alone. The
    input BOX is bottom-pinned and cannot scroll away:

      * box drawn + still holds unsubmitted glyphs -> ``'stuck'``  (re-send Enter)
      * box drawn + demonstrably empty             -> ``'submitted'``

    A stale 'Press up to edit queued messages' footer counts as success ONLY when
    the box is empty — a residual paste beneath it is still ``'stuck'``.
    """
    import re

    # 1. Residual text in the bottom-pinned box => NOT submitted, regardless of a
    #    scrolled statusline marker or a stale queued-messages footer.
    if input_box_stuck(bottom, head, ready_markers):
        return "stuck"
    # 2. Explicit positive markers (fast path, when still in the captured tail).
    if any(m in line for m in submission_markers for line in bottom.splitlines()):
        return "submitted"
    if active_pattern and re.search(active_pattern, bottom):
        return "submitted"
    if any(m in bottom for m in activity_markers):
        return "submitted"
    # 3. Queued footer with an EMPTY box => message accepted into the queue.
    if "Press up to edit queued messages" in bottom:
        return "submitted"
    # 4. Scroll-stable success: a drawn-and-empty input box proves the text left
    #    the box even when every statusline marker scrolled off behind a notice.
    if input_box_present(bottom) and not input_box_has_content(bottom):
        return "submitted"
    # 5. Legacy positive contract: readiness marker present + empty box.
    if any(m in bottom for m in ready_markers) and not input_box_has_content(bottom):
        return "submitted"
    return "unknown"


# --- watchdog fast-sweep predicate (used by juggle_watchdog.classify) ------


def is_unsubmitted(
    last_send_task_pane_hash,
    secs_since_dispatch,
    tail_hash,
    has_execution_markers,
    floor: float = FAST_SWEEP_FLOOR_SECS,
) -> bool:
    """The pane has not advanced one byte since dispatch (hash-equal to the
    post-dispatch snapshot), shows no execution markers, and is past ``floor``
    seconds → the prompt never submitted. Off when inputs are None."""
    return (
        last_send_task_pane_hash is not None
        and secs_since_dispatch is not None
        and secs_since_dispatch >= floor
        and not has_execution_markers
        and tail_hash == last_send_task_pane_hash
    )


def secs_since(last_send_task_at, now_ts: float) -> float | None:
    """Seconds between ``last_send_task_at`` (ISO string) and ``now_ts``; None if
    absent/unparseable so the fast-sweep stays off."""
    if not last_send_task_at:
        return None
    try:
        from datetime import datetime, timezone

        d = datetime.fromisoformat(last_send_task_at)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return now_ts - d.timestamp()
    except (ValueError, TypeError):
        return None


# --- watchdog fast-sweep recovery ladder (used by the daemon tick) ---------

# Keyed by "<agent_id>:<dispatch_hash>" so a fresh dispatch (new hash) restarts
# the ladder automatically — no explicit reset needed from the tick. Resets on
# daemon restart.
_ATTEMPTS: dict[str, int] = {}


def handle_unsubmitted(db, mgr, agent, pane_id: str, session_id: str) -> None:
    """Recover a paste-without-submit wedge in seconds.

    Rung 1 (first hit): the first C-m was swallowed by the paste race — resend it.
    Rung 2 (still wedged): Escape to dismiss the collapsed-paste block, then
    re-paste the task ONCE (verified synchronously by the hardened send_message).
    If that re-paste fails, escalate LOUD — a DISPATCH_FAILED event routed to the
    orchestrator (triage-ladder escalation per event_kinds).
    """
    agent_id = agent["id"]
    thread_id = agent.get("assigned_thread")
    key = f"{agent_id}:{agent.get('last_send_task_pane_hash')}"
    if _ATTEMPTS.get(key, 0) == 0:
        mgr._run_tmux("send-keys", "-t", pane_id, "C-m")
        _ATTEMPTS[key] = 1
        db.emit_event(
            thread_id=thread_id, session_id=session_id, kind=_ek.DISPATCH_FAILED,
            handled_by="watchdog",
            message=(f"[Watchdog] agent {agent_id[:8]} paste-not-submitted "
                     "(fast-sweep) — resent Enter (1/2)"),
        )
        _log.info("Watchdog: fast-sweep resent Enter to %s", agent_id[:8])
        return

    _ATTEMPTS.pop(key, None)
    mgr._run_tmux("send-keys", "-t", pane_id, "Escape")
    try:
        mgr.send_message(pane_id, agent.get("last_task") or "")
        db.emit_event(
            thread_id=thread_id, session_id=session_id, kind=_ek.DISPATCH_FAILED,
            handled_by="watchdog",
            message=(f"[Watchdog] agent {agent_id[:8]} paste-not-submitted "
                     "(fast-sweep) — Escape + re-pasted task (2/2, recovered)"),
        )
        _log.info("Watchdog: fast-sweep re-pasted task to %s", agent_id[:8])
    except Exception as e:
        db.emit_event(
            thread_id=thread_id, session_id=session_id, kind=_ek.DISPATCH_FAILED,
            handled_by="orchestrator",
            message=(f"[Watchdog] DISPATCH FAILED for agent {agent_id[:8]} — prompt "
                     f"never submitted after Enter + Escape/re-paste: {e}"),
        )
        _log.warning("Watchdog: fast-sweep DISPATCH FAILED for %s — %s", agent_id[:8], e)
