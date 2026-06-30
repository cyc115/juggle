"""Cockpit status-glyph single source of truth.

Owns every status glyph the cockpit renders (the six glyph dicts, moved here
from juggle_cockpit_view) + inline graph-marker constants, plus the STATUS_LEGEND
registry consumed by the ? Help modal and `cockpit --legend`. Renderers and the
help page read from THIS module so the legend can never drift (CLAUDE.md: one
source of truth).
"""
from __future__ import annotations

TOPIC_STATUS_GLYPHS: dict[str, str] = {
    "current": "👉",
    "running": "🏃",
    "paused": "⏸️",
    "done": "✅",
    "closed": "🔒",
    "failed": "❌",
    "archived": "🗄️",
    "active": "🔵",
    "background": "🏃",
}

ACTION_TIER_GLYPHS: dict[int, str] = {
    0: "⚠️",  # blocker
    1: "📬",  # review ready
    2: "❓",  # open question
    3: "📝",  # nudge/note
}

AGENT_STATUS_GLYPHS: dict[str, str] = {
    "busy": "🟢",
    "idle": "⚫",
    "stale": "🟡",
}

SCHED_STATUS_GLYPHS: dict[str, str] = {
    "running": "🔄",
    "ok": "✅",
    "failed": "❌",
    "unknown": "⏸️",
}

# Task-bound topics get their glyph from graph_tasks.state (autopilot, DA m2)
# — never from thread status/TTL, so done/failed tasks stay legible even after
# their threads close or archive.
TASK_STATE_GLYPHS: dict[str, str] = {
    "open": "⬡",
    "ready": "◇",
    "dispatching": "◌",
    "running": "🏃",
    "integrating": "🔀",
    "verified": "✅",
    "failed-exec": "❌",
    "failed-integration": "❌",
    "failed-verify": "❌",
    "blocked-failed": "🚫",
}

NOTIF_KIND_GLYPHS: dict[str, str] = {
    "complete": "⚡",
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "✗",
    "failed": "❌",
}

# Inline markers (graph cells / fallbacks) — referenced by renderers AND legend.
GRAPH_READY_SUFFIX = "▸"
GRAPH_DEP_SUFFIX = "⊣"
GRAPH_FAIL_SUFFIX = "✗"
MIRROR_PREFIX = "~"
UNREAD_BADGE = "⚠"
FALLBACK_TOPIC = "•"
FALLBACK_TASK = "⬢"
FALLBACK_SCHED = "⏰"
FALLBACK_NOTIF = "ℹ️"


def graph_inline_legend() -> str:
    """One-line Graph-panel legend, derived from TASK_STATE_GLYPHS + markers.
    Byte-equal to the legacy hardcoded string so the panel render is unchanged."""
    g = TASK_STATE_GLYPHS
    return (
        f"{g['running']} running  {g['ready']} ready  {g['open']} blocked  "
        f"{g['verified']} done  {g['failed-exec']} failed   "
        f"{GRAPH_DEP_SUFFIX}n=waits on #n"
    )
