"""juggle_frontier_render — pure text rendering helpers for the Frontier
Railroad screen (fr-screen, 2026-07-02; color/rails fidelity fr-vf-rails,
2026-07-03). Turns a FrontierLayout (see juggle_frontier_layout) + its
open-subgraph edges into Rich-markup body lines + a footer critical-path
summary. No Textual, no DB — extracted so juggle_cockpit_frontier_screen
stays a thin compose/bindings/actions module (architecture gate). Color
tokens and the rail gutter grid come from juggle_frontier_rails (single
source of truth for both)."""
from __future__ import annotations

from juggle_cockpit_legend import railroad_glyph
from juggle_frontier_rails import (
    AMBER, DIM, GREEN, RED, build_rail_gutters, gutter_plain, gutter_string,
)

_ROW_GLYPHS = {"running": "◐", "ready": "◇", "blocked": "○", "failed": "✗"}
_ROW_COLORS = {"running": AMBER, "ready": GREEN, "blocked": DIM, "failed": RED}
_STATE_COL_W = 8

# Narrow-viewport degradation thresholds (fr-smoke, 2026-07-02): agent info is
# the first thing dropped from a running row's meta, titles are truncated
# only once the pane is narrower still.
WIDTH_HIDE_AGENT = 100
WIDTH_TRUNCATE_TITLE = 60
_MIN_TITLE_LEN = 8


def _mk(text: str, color: "str | None", *, bold: bool = False) -> str:
    """Wrap ``text`` in Rich markup for ``color``/bold, escaping any literal
    ``[``/``]`` it contains (task ids like "[CR]" must never be interpreted
    as markup)."""
    escaped = text.replace("[", "\\[")
    styles = " ".join(s for s in (color, "bold" if bold else None) if s)
    if not styles:
        return escaped
    return f"[{styles}]{escaped}[/]"


def _truncate(text: str, width: "int | None") -> str:
    if width is None or width >= WIDTH_TRUNCATE_TITLE or len(text) <= _MIN_TITLE_LEN:
        return text
    return text[: max(_MIN_TITLE_LEN - 1, 1)] + "…"


def render_trunk_stub(anchor_count: int) -> str:
    noun = "topic" if anchor_count == 1 else "topics"
    label = f"{anchor_count} verified {noun}" if anchor_count else "0 verified"
    return _mk(f"▦ trunk · {label}", DIM)


def render_wave_band(row, lane_count: int) -> str:
    label = f"┄┄ wave {row.wave} ┄┄ ({row.parallel_count} parallel)"
    if row.free_slots is not None:
        label += f" — {row.parallel_count} can start in parallel, {row.free_slots} slots free -> {row.queued} queues"
    return _mk(label, DIM)


def render_fold_summary(row) -> str:
    return _mk(f"z  …wave {row.wave} folded ({row.parallel_count} nodes) — z expands", DIM)


def _dep_name_color(dep_id: str, dep_states: "dict | None", critical_titles: "set | None") -> str:
    if critical_titles and dep_id in critical_titles:
        return AMBER
    if dep_states and dep_states.get(dep_id) in ("ready", "verified"):
        return GREEN
    return DIM


def _state_col(row, meta: "dict | None") -> str:
    if row.kind == "ready":
        return _mk("ready".ljust(_STATE_COL_W), GREEN)
    if row.kind == "failed":
        return _mk("failed".ljust(_STATE_COL_W), RED)
    if row.kind == "running":
        elapsed = (meta or {}).get("elapsed") or ""
        text = f"▶ {elapsed}".strip()
        return _mk(text.ljust(_STATE_COL_W), AMBER)
    return " " * _STATE_COL_W  # blocked: detail lives in meta ("waits on ...")


def _meta_col(row, meta: "dict | None", width: "int | None") -> str:
    if row.kind == "running" and meta:
        parts = [p for p in (meta.get("agent"),) if p and (width is None or width >= WIDTH_HIDE_AGENT)]
        text = " · ".join(parts)
    elif row.kind == "blocked" and row.blocked_on:
        text = "waits on " + ", ".join(row.blocked_on)
    else:
        text = ""
    if row.tasks_done is not None and row.tasks_total is not None:
        counts = f"{row.tasks_done}/{row.tasks_total}"
        text = f"{text} · {counts}" if text else counts
    return _mk(text, DIM) if text else ""


def render_node_row(
    row, lane_count: int, *, selected: bool, meta: "dict | None" = None,
    width: "int | None" = None, gutter_cells: "list | None" = None,
    title_w: "int | None" = None,
) -> str:
    """``meta`` (running rows only): {"elapsed": "12m", "agent": "coder·sonnet"}.
    ``width`` drives narrow-viewport degradation — agent info drops first
    (below WIDTH_HIDE_AGENT), then the title truncates (below
    WIDTH_TRUNCATE_TITLE). ``gutter_cells`` is this row's slice of the rail
    grid (juggle_frontier_rails.build_rail_gutters); ``title_w`` pads the
    title column to the widest visible row's plain length."""
    cursor = _mk("▶", AMBER, bold=True) if selected else " "
    gutter = gutter_string(gutter_cells) if gutter_cells is not None else " " * lane_count
    glyph_char = _ROW_GLYPHS.get(row.kind) or railroad_glyph(row.state)
    glyph = _mk(glyph_char, _ROW_COLORS.get(row.kind))
    title = _truncate(row.title, width)
    pad = max((title_w or len(title)) - len(title), 0)
    title_cell = _mk(title, None, bold=True) + " " * pad
    state_cell = _state_col(row, meta)
    meta_cell = _meta_col(row, meta, width)
    return f"{cursor}{gutter}{glyph} {title_cell} {state_cell} {meta_cell}".rstrip()


def render_rows(
    layout, edges: "list | None" = None, *, selected_id: "str | None" = None,
    meta_by_id: "dict | None" = None, width: "int | None" = None,
    plain: bool = False,
) -> list[str]:
    """Render the full body: trunk stub + every layout row in order. Rail
    gutters are built ONCE over the whole layout (juggle_frontier_rails) so
    verticals/elbows/pass-through bands line up across rows. ``plain=True``
    strips Rich markup (via a lightweight bracket-stripper — no Rich import
    here) so existing plain-text callers/tests keep working; column padding
    is always computed on plain widths regardless."""
    meta_by_id = meta_by_id or {}
    grid = build_rail_gutters(layout, edges or [])
    node_rows = [r for r in layout.rows if r.kind in _ROW_GLYPHS]
    title_w = max((len(_truncate(r.title, width)) for r in node_rows), default=0)

    lines = [render_trunk_stub(layout.anchor_count)]
    for i, row in enumerate(layout.rows):
        cells = grid[i] if i < len(grid) else None
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
                gutter_cells=cells,
                title_w=title_w,
            ))
    if plain:
        lines = [strip_markup(line) for line in lines]
    return lines


def strip_markup(text: str) -> str:
    """Strip ``[style]...[/]`` Rich markup spans (and unescape ``\\[``) without
    importing Rich, for callers that need pane-width math on plain text."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "[":
            out.append("[")
            i += 2
            continue
        if ch == "[":
            end = text.find("]", i)
            if end != -1:
                i = end + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def render_critical_path_footer(layout, titles_by_id: "dict | None" = None) -> str:
    if not layout.critical_path:
        return _mk("critical path — none (nothing open)", DIM)
    titles_by_id = titles_by_id or {}
    names = " > ".join(titles_by_id.get(nid, nid) for nid in layout.critical_path)
    waves = len({r.wave for r in layout.rows if r.id in set(layout.critical_path)})
    return _mk("critical path ", AMBER) + _mk(names, AMBER) + _mk(f" — {waves} waves to done", DIM)
