"""juggle_frontier_render — pure text rendering helpers for the Frontier
Railroad screen (fr-screen, 2026-07-02). Turns a FrontierLayout (see
juggle_frontier_layout) into plain-text body lines + a footer critical-path
summary. No Textual, no DB, no Rich — extracted so juggle_cockpit_frontier_screen
stays a thin compose/bindings/actions module (architecture gate)."""
from __future__ import annotations

from juggle_cockpit_legend import railroad_glyph

_ROW_GLYPHS = {"running": "◐", "ready": "◇", "blocked": "○", "failed": "✗"}

# Narrow-viewport degradation thresholds (fr-smoke, 2026-07-02): agent info is
# the first thing dropped from a running row's meta, titles are truncated
# only once the pane is narrower still.
WIDTH_HIDE_AGENT = 100
WIDTH_TRUNCATE_TITLE = 60
_MIN_TITLE_LEN = 8


def _truncate(text: str, width: "int | None") -> str:
    if width is None or width >= WIDTH_TRUNCATE_TITLE or len(text) <= _MIN_TITLE_LEN:
        return text
    return text[: max(_MIN_TITLE_LEN - 1, 1)] + "…"


def _gutter(lane: int, lane_count: int, on_critical: bool) -> str:
    """Forward-dependency rail prefix: one column per lane, the row's own
    lane rendered as a double-line "║" when it sits on the critical path,
    single "│" otherwise; other columns are blank (no occupancy tracing)."""
    cols = ["  "] * max(lane_count, lane + 1)
    cols[lane] = ("║ " if on_critical else "│ ")
    return "".join(cols)


def render_trunk_stub(anchor_count: int) -> str:
    noun = "topic" if anchor_count == 1 else "topics"
    return f"● {anchor_count} verified {noun}" if anchor_count else "● 0 verified"


def render_wave_band(row, lane_count: int) -> str:
    label = f"┄┄ wave {row.wave} ┄┄ ({row.parallel_count} parallel)"
    if row.free_slots is not None:
        label += f" — {row.parallel_count} can start in parallel, {row.free_slots} slots free -> {row.queued} queues"
    return label


def render_fold_summary(row) -> str:
    return f"z  …wave {row.wave} folded ({row.parallel_count} nodes) — z expands"


def render_node_row(
    row, lane_count: int, *, selected: bool, meta: "dict | None" = None,
    width: "int | None" = None,
) -> str:
    """``meta`` (running rows only): {"elapsed": "12m", "agent": "coder·sonnet"}.
    ``width`` drives narrow-viewport degradation — agent info drops first
    (below WIDTH_HIDE_AGENT), then the title truncates (below
    WIDTH_TRUNCATE_TITLE)."""
    cursor = "▶" if selected else " "
    gutter = _gutter(row.lane, lane_count, row.on_critical_path)
    glyph = _ROW_GLYPHS.get(row.kind) or railroad_glyph(row.state)
    title = _truncate(row.title, width)
    detail = ""
    if row.kind == "running" and meta:
        parts = [p for p in (meta.get("elapsed"),) if p]
        if meta.get("agent") and (width is None or width >= WIDTH_HIDE_AGENT):
            parts.append(meta["agent"])
        if parts:
            detail = "  " + "  ".join(parts)
    elif row.kind == "blocked" and row.blocked_on:
        detail = f"  waits on {', '.join(row.blocked_on)}"
    counts = ""
    if row.tasks_done is not None and row.tasks_total is not None:
        counts = f"  {row.tasks_done}/{row.tasks_total}"
    return f"{cursor}{gutter}{glyph} {title}{counts}{detail}"


def render_rows(
    layout, *, selected_id: "str | None" = None, meta_by_id: "dict | None" = None,
    width: "int | None" = None,
) -> list[str]:
    """Render the full body: trunk stub + every layout row in order."""
    meta_by_id = meta_by_id or {}
    lines = [render_trunk_stub(layout.anchor_count)]
    for row in layout.rows:
        if row.kind == "wave-band":
            lines.append(render_wave_band(row, layout.lane_count))
        elif row.kind == "fold-summary":
            lines.append(render_fold_summary(row))
        else:
            lines.append(render_node_row(
                row, layout.lane_count,
                selected=(row.id == selected_id),
                meta=meta_by_id.get(row.id),
                width=width,
            ))
    return lines


def render_critical_path_footer(layout, titles_by_id: "dict | None" = None) -> str:
    if not layout.critical_path:
        return "critical path — none (nothing open)"
    titles_by_id = titles_by_id or {}
    names = " > ".join(titles_by_id.get(nid, nid) for nid in layout.critical_path)
    waves = len({r.wave for r in layout.rows if r.id in set(layout.critical_path)})
    return f"critical path {names} — {waves} waves to done"
