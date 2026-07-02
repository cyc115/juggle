"""Spawn-readiness marker plumbing for agent panes.

Extracted from juggle_tmux (LOC/architecture gate) so the readiness/submission
markers, harness-marker resolution, and the watchdog-heartbeat refresh live in
one small, single-purpose module. juggle_tmux imports these back.
"""
from __future__ import annotations

import os

# Built-in Claude Code markers — used as the fallback when the configured
# harness adapter can't be resolved (see _harness_markers).
#
# "shift+tab to cycle" is the STABLE structural marker of a ready interactive
# input box: it renders in every permission mode ("accept edits on ... (shift+tab
# to cycle)", "bypass permissions on ... (shift+tab to cycle)") and for every
# model, and persists when the pane is idle — unlike the transient "/effort"
# widget or the mode-specific "bypass permissions on" text (defect E, 2026-07-01:
# juggle spawns in accept-edits mode, so those two never reliably matched).
_READY_MARKERS = ("shift+tab to cycle", "bypass permissions on", "/effort")
_SUBMISSION_MARKERS = ("esc to interrupt", "✻", "✶")

# Claude Code v2.1.198 (2026-07-01) shows a "Quick safety check" folder-trust
# dialog on FIRST launch in any untrusted dir — EVEN with
# --dangerously-skip-permissions. Fresh worktrees are always untrusted, so a
# pane blocks here until the readiness timeout (defect E). Pre-trust
# (juggle_claude_trust) normally prevents it; these markers let the readiness
# prober recognize the dialog as a BACKSTOP and answer it (Enter accepts the
# default "Yes, I trust this folder"). Kept distinct from ready markers so the
# dialog is never mistaken for a ready pane.
_TRUST_PROMPT_MARKERS = ("Quick safety check", "Yes, I trust this folder")
# Markers indicating the agent is already processing (consumed prompt, started tool calls).
# Used in wait_for_submission to detect the fast-agent false-negative: prompt left the
# input box before the first poll, so _SUBMISSION_MARKERS are gone but agent is running.
_ACTIVITY_MARKERS = ("⏺",)


# Best-effort watchdog-heartbeat refresh, bound at import so tests can patch it.
# A long spawn-readiness wait runs INSIDE the watchdog tick; without a refresh
# the heartbeat goes stale and CLI calls warn "watchdog not running" (defect E).
try:
    from juggle_watchdog_health import write_heartbeat  # noqa: F401
except Exception:  # pragma: no cover — health module always importable in practice
    write_heartbeat = None  # type: ignore[assignment]


def _beat_heartbeat_if_watchdog() -> None:
    """Refresh the watchdog heartbeat when running inside the watchdog process.

    Guarded by ``JUGGLE_WATCHDOG_SANCTIONED`` (set only in the watchdog child)
    so a plain CLI dispatch never touches the heartbeat — that would mask a
    genuinely dead watchdog. Never raises."""
    if os.environ.get("JUGGLE_WATCHDOG_SANCTIONED") != "1" or write_heartbeat is None:
        return
    try:
        write_heartbeat()
    except Exception:
        pass


def _harness_markers():
    """Return ``(readiness, submission)`` marker tuples for the default harness.

    Resolved from the GLOBAL default harness (``agent.harness``) via
    ``juggle_harness.get_adapter`` — panes don't carry their harness id, so a
    mixed per-role harness setup shares these markers. Falls back to the
    built-in Claude markers if the adapter can't be resolved.
    """
    try:
        from juggle_harness import get_adapter

        adapter = get_adapter()
        return (
            adapter.readiness_markers() or _READY_MARKERS,
            adapter.submission_markers() or _SUBMISSION_MARKERS,
        )
    except Exception:
        return _READY_MARKERS, _SUBMISSION_MARKERS
