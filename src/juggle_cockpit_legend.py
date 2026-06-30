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


# ---------------------------------------------------------------------------
# STATUS_LEGEND registry — the ? help / `cockpit --legend` content. Glyph values
# are READ from the dicts/constants above (never re-typed) so the legend is
# mechanically tied to what the renderers draw.
# ---------------------------------------------------------------------------

STATUS_LEGEND: list[dict] = [
    {"section": "Topics", "entries": [
        {"glyph": TOPIC_STATUS_GLYPHS["current"],  "meaning": "here — current thread"},
        {"glyph": TOPIC_STATUS_GLYPHS["running"],  "meaning": "running / background agent"},
        {"glyph": TOPIC_STATUS_GLYPHS["paused"],   "meaning": "paused"},
        {"glyph": TOPIC_STATUS_GLYPHS["done"],     "meaning": "done"},
        {"glyph": TOPIC_STATUS_GLYPHS["closed"],   "meaning": "closed"},
        {"glyph": TOPIC_STATUS_GLYPHS["failed"],   "meaning": "failed"},
        {"glyph": TOPIC_STATUS_GLYPHS["archived"], "meaning": "archived"},
        {"glyph": TOPIC_STATUS_GLYPHS["active"],   "meaning": "active (live, not foregrounded)"},
        {"glyph": GRAPH_READY_SUFFIX,              "meaning": "project section header"},
        {"glyph": FALLBACK_TASK,                   "meaning": "project task-progress prefix"},
        {"glyph": "[↑N]",                          "meaning": "pane scrolled down N rows"},
    ]},
    {"section": "Action Items", "entries": [
        {"glyph": ACTION_TIER_GLYPHS[0], "meaning": "blocker (highest priority)"},
        {"glyph": ACTION_TIER_GLYPHS[1], "meaning": "review ready"},
        {"glyph": ACTION_TIER_GLYPHS[2], "meaning": "open question"},
        {"glyph": ACTION_TIER_GLYPHS[3], "meaning": "nudge / note"},
    ]},
    {"section": "Agents", "entries": [
        {"glyph": AGENT_STATUS_GLYPHS["busy"],  "meaning": "busy"},
        {"glyph": AGENT_STATUS_GLYPHS["idle"],  "meaning": "idle"},
        {"glyph": AGENT_STATUS_GLYPHS["stale"], "meaning": "stale (no recent activity)"},
        {"glyph": "#N",  "meaning": "1-based agent index (f / d / t keys)"},
        {"glyph": "(A)", "meaning": "agent — role follows"},
        {"glyph": "(L)", "meaning": "launchd / scheduled task — label follows"},
        {"glyph": SCHED_STATUS_GLYPHS["running"], "meaning": "scheduled task running"},
        {"glyph": SCHED_STATUS_GLYPHS["ok"],      "meaning": "scheduled task last run ok"},
        {"glyph": SCHED_STATUS_GLYPHS["failed"],  "meaning": "scheduled task last run failed"},
        {"glyph": FALLBACK_SCHED,                 "meaning": "scheduled task (status unknown)"},
    ]},
    {"section": "Notifications", "entries": [
        {"glyph": NOTIF_KIND_GLYPHS["complete"], "meaning": "complete"},
        {"glyph": NOTIF_KIND_GLYPHS["info"],     "meaning": "info"},
        {"glyph": NOTIF_KIND_GLYPHS["warning"],  "meaning": "warning"},
        {"glyph": NOTIF_KIND_GLYPHS["error"],    "meaning": "error"},
        {"glyph": NOTIF_KIND_GLYPHS["failed"],   "meaning": "failed"},
    ]},
    {"section": "Graph (task DAG)", "entries": [
        {"glyph": TASK_STATE_GLYPHS["open"],           "meaning": "open — blocked on deps"},
        {"glyph": TASK_STATE_GLYPHS["ready"],          "meaning": "ready — next up"},
        {"glyph": TASK_STATE_GLYPHS["dispatching"],    "meaning": "dispatching"},
        {"glyph": TASK_STATE_GLYPHS["running"],        "meaning": "running"},
        {"glyph": TASK_STATE_GLYPHS["integrating"],    "meaning": "integrating"},
        {"glyph": TASK_STATE_GLYPHS["verified"],       "meaning": "verified (done)"},
        {"glyph": TASK_STATE_GLYPHS["failed-exec"],    "meaning": "failed (exec / integration / verify)"},
        {"glyph": TASK_STATE_GLYPHS["blocked-failed"], "meaning": "blocked by a failed dependency"},
        {"glyph": GRAPH_DEP_SUFFIX + "n",              "meaning": "waiting on task #n"},
        {"glyph": GRAPH_FAIL_SUFFIX,                   "meaning": "failed-state suffix"},
        {"glyph": MIRROR_PREFIX + "name",              "meaning": "mirror task (cross-project dep)"},
        {"glyph": "N/M",                               "meaning": "sub-DAG progress (done/total)"},
        {"glyph": UNREAD_BADGE + "n",                  "meaning": "n unread notifications (graph mode)"},
        {"glyph": "▕█░▏",                              "meaning": "done-fraction progress bar"},
    ]},
]


def build_legend_content() -> list[dict]:
    """Authoritative legend content (tests assert on this)."""
    return STATUS_LEGEND


def all_rendered_glyphs() -> set[str]:
    """Every glyph value a panel can emit — coverage-pin denominator."""
    glyphs: set[str] = set()
    for d in (TOPIC_STATUS_GLYPHS, AGENT_STATUS_GLYPHS, SCHED_STATUS_GLYPHS,
              TASK_STATE_GLYPHS, NOTIF_KIND_GLYPHS):
        glyphs |= set(d.values())
    glyphs |= set(ACTION_TIER_GLYPHS.values())
    return glyphs


def render_legend_lines(width: int = 76) -> list[str]:
    """Dense multi-column legend text (AMENDMENT 1): pack 2–3 '<glyph> <meaning>'
    cells per row so the legend mostly fits one screen; the ? modal scrolls as a
    safety net. ``width`` drives the column count. Every glyph+meaning appears so
    `cockpit --legend` stdout stays complete (mirrors render_help_lines' style)."""
    cols = 3 if width >= 102 else (2 if width >= 56 else 1)
    cell_w = max(24, width // cols)
    lines: list[str] = ["", "Status Legend", "─" * 52]
    for sec in STATUS_LEGEND:
        lines.append("")
        lines.append(f"  {sec['section']}")
        lines.append("  " + "─" * 48)
        cells = [f"{e['glyph']}  {e['meaning']}" for e in sec["entries"]]
        for i in range(0, len(cells), cols):
            row = cells[i:i + cols]
            # ljust each cell except the last; an over-long cell still keeps a
            # 2-space gap before the next column so cells never run together.
            packed = "".join(
                (c.ljust(cell_w) if len(c) < cell_w else c + "  ") if j < len(row) - 1 else c
                for j, c in enumerate(row)
            )
            lines.append("  " + packed.rstrip())
    return lines
