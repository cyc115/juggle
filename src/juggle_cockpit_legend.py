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
    0: "🛑",  # blocker / needs-you (canonical vocab: ⚠️→🛑, ⚠️=warn only)
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
    "running": "🔄",  # documented alias of canonical running 🏃
    "ok": "✅",
    "failed": "❌",
    "unknown": "❔",  # canonical vocab: ⏸️→❔ (⏸️=paused only)
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

# Single-cell glyphs for the horizontally-compressed dependency spine / railroad
# (2026-06-30 graph railroad). TASK_STATE_GLYPHS is emoji (double-width) and
# unusable inline — this is the compact twin, same source module (single source).
RAILROAD_STATE_GLYPHS: dict[str, str] = {
    "open": "·", "ready": "○", "dispatching": "◐", "running": "◐",
    "integrating": "◐", "verified": "●",
    "failed-exec": "✗", "failed-integration": "✗", "failed-verify": "✗",
    "blocked-failed": "⊘",  # canonical vocab: ◇→⊘ (◇/○ = ready everywhere)
}


def railroad_glyph(state: str) -> str:
    return RAILROAD_STATE_GLYPHS.get(state, "·")


NOTIF_KIND_GLYPHS: dict[str, str] = {
    "complete": "⚡",
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "❌",  # canonical vocab: ✗→❌ (error is a failure; ✗ = compact only)
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


# ---------------------------------------------------------------------------
# Canonical lifecycle vocabulary (CANONICAL_VOCAB.md). One glyph per universal
# lifecycle state — an emoji track (panels / legend, double-width) and a compact
# track (single-cell inline spine / railroad). The six domain dicts above re-map
# onto these; a coverage test (test_no_glyph_denotes_two_lifecycle_states)
# asserts no glyph names two universal states, so the vocab can never re-drift.
# ---------------------------------------------------------------------------
CANONICAL_GLYPHS: dict[str, str] = {
    "running": "🏃",
    "ready": "◇",
    "done": "✅",
    "failed": "❌",
    "blocked": "🚫",
    "needs-you": "🛑",
    "paused": "⏸️",
    "unknown": "❔",
    "info": "ℹ️",
    "warn": "⚠️",
}

CANONICAL_COMPACT: dict[str, str] = {
    "running": "◐",
    "ready": "○",
    "done": "●",
    "failed": "✗",
    "blocked": "⊘",
}

# Which universal state each concrete domain state rolls up to. Kind glyphs
# (identity, not lifecycle) are intentionally excluded. This is the checked-
# against-the-dicts single source that keeps the vocab drift-free.
_LIFECYCLE_ROLLUP: list[tuple[dict, dict]] = [
    (TOPIC_STATUS_GLYPHS, {
        "running": "running", "background": "running", "active": "running",
        "paused": "paused", "done": "done", "failed": "failed"}),
    (ACTION_TIER_GLYPHS, {0: "needs-you"}),
    (AGENT_STATUS_GLYPHS, {"busy": "running"}),
    (SCHED_STATUS_GLYPHS, {
        "running": "running", "ok": "done", "failed": "failed",
        "unknown": "unknown"}),
    (TASK_STATE_GLYPHS, {
        "ready": "ready", "dispatching": "running", "running": "running",
        "integrating": "running", "verified": "done",
        "failed-exec": "failed", "failed-integration": "failed",
        "failed-verify": "failed", "open": "blocked", "blocked-failed": "blocked"}),
    (RAILROAD_STATE_GLYPHS, {
        "ready": "ready", "dispatching": "running", "running": "running",
        "integrating": "running", "verified": "done",
        "failed-exec": "failed", "failed-integration": "failed",
        "failed-verify": "failed", "open": "blocked", "blocked-failed": "blocked"}),
    (NOTIF_KIND_GLYPHS, {
        "info": "info", "warning": "warn", "error": "failed", "failed": "failed"}),
]


def glyph_to_universal_states() -> dict[str, set[str]]:
    """Map every lifecycle glyph → the set of universal states it denotes.
    A well-formed vocab yields exactly one universal per glyph (kind glyphs,
    which carry identity not lifecycle, are excluded)."""
    out: dict[str, set[str]] = {}
    for domain, rollup in _LIFECYCLE_ROLLUP:
        for state, universal in rollup.items():
            out.setdefault(domain[state], set()).add(universal)
    return out


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
        {"glyph": AGENT_STATUS_GLYPHS["busy"],  "meaning": "busy — agent running"},
        {"glyph": AGENT_STATUS_GLYPHS["idle"],  "meaning": "idle"},
        {"glyph": AGENT_STATUS_GLYPHS["stale"], "meaning": "stale (no recent activity)"},
        {"glyph": "#N",  "meaning": "1-based agent index (f / d / t keys)"},
        {"glyph": "(A)", "meaning": "agent — role follows"},
        {"glyph": "(L)", "meaning": "launchd / scheduled task — label follows"},
        {"glyph": SCHED_STATUS_GLYPHS["running"], "meaning": "scheduled task running"},
        {"glyph": SCHED_STATUS_GLYPHS["ok"],      "meaning": "scheduled task last run ok"},
        {"glyph": SCHED_STATUS_GLYPHS["failed"],  "meaning": "scheduled task last run failed"},
        {"glyph": SCHED_STATUS_GLYPHS["unknown"], "meaning": "scheduled task status unknown"},
        {"glyph": FALLBACK_SCHED,                 "meaning": "scheduled task (unrecognized status)"},
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
    """Status Legend v2 (approved redesign): regrouped, PER-SECTION 2-column
    where the cells fit (e.g. Notifications) and 1-column where they don't
    (e.g. Agents), so the legend mostly fits one screen; the ? modal scrolls as
    a safety net. Every glyph+meaning still appears so `cockpit --legend` stdout
    stays complete. ``width`` is the usable text width."""
    rule = "─" * width
    lines: list[str] = ["", "Status Legend", rule]
    for sec in STATUS_LEGEND:
        cells = [f"{e['glyph']} {e['meaning']}" for e in sec["entries"]]
        widest = max((len(c) for c in cells), default=0)
        # 2-col only when the widest cell fits twice across the width.
        cols = 2 if widest <= (width - 2) // 2 - 2 else 1
        col_w = (width - 2) // cols
        lines.append("")
        lines.append(f"  {sec['section']}")
        for i in range(0, len(cells), cols):
            row = cells[i:i + cols]
            packed = "".join(
                (c.ljust(col_w) if j < len(row) - 1 else c)
                for j, c in enumerate(row)
            )
            lines.append("  " + packed.rstrip())
    return lines
