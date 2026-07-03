"""juggle_frontier_render — pure text rendering helpers for the Frontier
Railroad screen (fr-screen, 2026-07-02; visual fidelity fr-vf-rails +
fr-vf-polish, 2026-07-03). Turns a FrontierLayout (see juggle_frontier_layout)
+ its open-subgraph edges into Rich-markup body lines: trunk stub, node rows,
wave bands (with pass-through rails), fold summaries, and critical-path /
legend footers. No Textual, no DB — extracted so juggle_cockpit_frontier_screen
stays a thin compose/bindings/actions module (architecture gate). Color
tokens, escaping, and the rail gutter grid all come from juggle_frontier_rails
(single source of truth)."""
from __future__ import annotations

from juggle_cockpit_legend import railroad_glyph
from juggle_frontier_rails import (
    AMBER, DIM, GREEN, RED, build_rail_gutters, gutter_string, source_color, style,
)

_ROW_GLYPHS = {"running": "◐", "ready": "◇", "blocked": "○", "failed": "✗"}
_ROW_COLORS = {"running": AMBER, "ready": GREEN, "blocked": DIM, "failed": RED}
_STATE_COL_W = 8
_DEFAULT_RULE_WIDTH = 76

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


def render_trunk_stub(anchor_count: int, lane_count: int = 1) -> list[str]:
    """Two-line anchor header: a labeled stub feeding a DIM vertical rail
    that visually anchors "every rail below grows from here" into wave 1."""
    noun = "topic" if anchor_count == 1 else "topics"
    label = f"{anchor_count} verified {noun}" if anchor_count else "0 verified"
    head = style(f"▦ trunk · {label} ────┬──── every rail below grows from here", DIM)
    cols = [" "] * max(lane_count, 1)
    cols[0] = style("│", DIM)
    return [head, "".join(cols)]


def _band_gutter(gutter_cells: "list | None", lane_count: int) -> str:
    return gutter_string(gutter_cells) if gutter_cells is not None else " " * lane_count


def render_wave_band(row, lane_count: int, *, gutter_cells: "list | None" = None, width: "int | None" = None) -> str:
    """Full-width dotted rule with an inline capacity annotation. Wave 1 is
    "runnable now" and shows the dispatch-capacity readout (possible ‖ slots
    free [-> queued]); later waves are "after wave N-1" with a bare parallel
    count. Live edges passing through this band keep their rail char in the
    gutter prefix (band renderer takes the pre-computed lane occupancy as
    input — pure, deterministic golden tests)."""
    gutter = _band_gutter(gutter_cells, lane_count)
    gutter_plain_w = len(gutter_cells) if gutter_cells is not None else lane_count
    when = "runnable now" if row.wave == 1 else f"after wave {row.wave - 1}"
    prefix = f"┄┄ wave {row.wave} · {when} "
    if row.free_slots is not None:
        ann = f"{row.parallel_count} ‖ possible · {row.free_slots} slots free"
        if row.queued:
            ann += f" -> {row.queued} queue"
    else:
        ann = f"({row.parallel_count} parallel)"
    total_w = width or _DEFAULT_RULE_WIDTH
    # Fill length MUST use the gutter's PLAIN width (cell count), never the
    # markup string length -- markup byte-length vs. visible width is the
    # classic footgun here (colored pass-through cells add "[color]...[/]"
    # bytes that don't occupy screen columns).
    fill = max(4, total_w - gutter_plain_w - len(prefix) - len(ann) - 2)
    rule = prefix + ("┄" * fill) + "  " + ann
    return gutter + style(rule, DIM)


def render_fold_summary(row, *, gutter_cells: "list | None" = None, lane_count: int = 1) -> str:
    gutter = _band_gutter(gutter_cells, lane_count)
    return gutter + style(f"z  …wave {row.wave} folded ({row.parallel_count} nodes) — z expands", DIM)


def _state_col(row, meta: "dict | None") -> str:
    if row.kind == "ready":
        return style("ready".ljust(_STATE_COL_W), GREEN)
    if row.kind == "failed":
        return style("failed".ljust(_STATE_COL_W), RED)
    if row.kind == "running":
        elapsed = (meta or {}).get("elapsed") or ""
        text = f"▶ {elapsed}".strip()
        return style(text.ljust(_STATE_COL_W), AMBER)
    return " " * _STATE_COL_W  # blocked: detail lives in meta ("waits on ...")


def _colored_dep_names(names, dep_state: "dict | None", critical_titles: "set | None") -> str:
    dep_state = dep_state or {}
    critical_titles = critical_titles or set()
    parts = []
    for name in names:
        on_cp = name in critical_titles
        color = source_color(dep_state.get(name, ""), on_cp)
        parts.append(style(name, color))
    return ", ".join(parts)


def _meta_col(
    row, meta: "dict | None", width: "int | None",
    dep_state: "dict | None" = None, critical_titles: "set | None" = None,
) -> str:
    if row.kind == "running" and meta:
        parts = [p for p in (meta.get("agent"),) if p and (width is None or width >= WIDTH_HIDE_AGENT)]
        text = style(" · ".join(parts), DIM) if parts else ""
    elif row.kind == "blocked" and row.blocked_on:
        text = style("waits on ", DIM) + _colored_dep_names(row.blocked_on, dep_state, critical_titles)
    else:
        text = ""
    if row.tasks_done is not None and row.tasks_total is not None:
        counts = style(f"{row.tasks_done}/{row.tasks_total}", DIM)
        text = f"{text} {style('·', DIM)} {counts}" if text else counts
    return text


def render_node_row(
    row, lane_count: int, *, selected: bool, meta: "dict | None" = None,
    width: "int | None" = None, gutter_cells: "list | None" = None,
    title_w: "int | None" = None, dep_state: "dict | None" = None,
    critical_titles: "set | None" = None,
) -> str:
    """``meta`` (running rows only): {"elapsed": "12m", "agent": "coder·sonnet"}.
    ``width`` drives narrow-viewport degradation — agent info drops first
    (below WIDTH_HIDE_AGENT), then the title truncates (below
    WIDTH_TRUNCATE_TITLE). ``gutter_cells`` is this row's slice of the rail
    grid (juggle_frontier_rails.build_rail_gutters); ``title_w`` pads the
    title column to the widest visible row's plain length; ``dep_state`` /
    ``critical_titles`` color a blocked row's "waits on" dependency names."""
    cursor = style("▶", AMBER, bold=True) if selected else " "
    gutter = gutter_string(gutter_cells) if gutter_cells is not None else " " * lane_count
    glyph_char = _ROW_GLYPHS.get(row.kind) or railroad_glyph(row.state)
    glyph = style(glyph_char, _ROW_COLORS.get(row.kind))
    title = _truncate(row.title, width)
    pad = max((title_w or len(title)) - len(title), 0)
    title_cell = style(title, None, bold=True) + " " * pad
    state_cell = _state_col(row, meta)
    meta_cell = _meta_col(row, meta, width, dep_state, critical_titles)
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
    dep_state = {r.title: r.state for r in node_rows}
    critical_titles = {r.title for r in node_rows if r.on_critical_path}

    lines = render_trunk_stub(layout.anchor_count, layout.lane_count)
    for i, row in enumerate(layout.rows):
        cells = grid[i] if i < len(grid) else None
        if row.kind == "wave-band":
            lines.append(render_wave_band(row, layout.lane_count, gutter_cells=cells, width=width))
        elif row.kind == "fold-summary":
            lines.append(render_fold_summary(row, gutter_cells=cells, lane_count=layout.lane_count))
        else:
            lines.append(render_node_row(
                row, layout.lane_count,
                selected=(row.id == selected_id),
                meta=meta_by_id.get(row.id),
                width=width,
                gutter_cells=cells,
                title_w=title_w,
                dep_state=dep_state,
                critical_titles=critical_titles,
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
        return style("critical path — none (nothing open)", DIM)
    titles_by_id = titles_by_id or {}
    names = " ▸ ".join(titles_by_id.get(nid, nid) for nid in layout.critical_path)
    waves = len({r.wave for r in layout.rows if r.id in set(layout.critical_path)})
    return (
        style("critical rail ║  ", AMBER) + style(names, AMBER)
        + style(f" — {waves} waves to done", DIM)
    )


def render_legend_footer() -> str:
    legend = (
        f"{style('◐', AMBER)} running  {style('◇', GREEN)} ready  "
        f"{style('○', DIM)} blocked  {style('✗', RED)} failed"
    )
    keys = "j/k select · Enter open · z fold wave · Tab project"
    return legend + style(f" · {keys}", DIM)
