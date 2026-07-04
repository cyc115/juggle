"""juggle_plan_render — pure text renderer for the cockpit Plan view
(cockpit-plan-view, 2026-07-02). Turns a juggle_plan_layout.PlanLayout into
Rich-markup body lines laid out as dependency-wave COLUMNS: a pinned anchor
stub (left), then one column per wave (longest-path depth) with a per-column
"WAVE n" header and stacked cards, a full-width capacity readout for the
runnable-now wave, a fold summary, and critical-path / legend footers.

The card GRID is rendered as plain fixed-width columns and zipped row-by-row —
every column is padded to the tallest column's height before zipping, so a wide
fan-in wave (15+ cards) can never index past a shorter column (the exact class
of IndexError the removed gitlog railroad hit). Color is confined to the
full-width header/footer lines (never the zipped grid) so visible-width math
stays trivial. No Textual, no DB — fully unit-testable.
"""
from __future__ import annotations

from juggle_frontier_rails import AMBER, DIM, GREEN, RED, style

CARD_W = 18
_INNER = CARD_W - 2
COL_GAP = "   "  # gutter between wave columns
_STRIDE = CARD_W + len(COL_GAP)

_GLYPH = {"running": "◐", "ready": "◇", "blocked": "○", "failed": "✗"}


def _fit(text: str, w: int = CARD_W) -> str:
    """Truncate-or-pad ``text`` to exactly ``w`` visible plain columns."""
    if len(text) > w:
        return text[: max(w - 1, 0)] + "…" if w > 1 else text[:w]
    return text.ljust(w)


def _state_text(card, meta: "dict | None") -> str:
    if card.kind == "running":
        meta = meta or {}
        bits = [f"▶ {meta.get('elapsed') or '0m'}"]
        if meta.get("agent"):
            bits.append(meta["agent"])
        return " ".join(bits)
    if card.kind == "blocked":
        return "needs " + ", ".join(card.blocked_on) if card.blocked_on else "blocked"
    if card.kind == "failed":
        return "failed"
    return "ready"


def _card_lines(card, *, selected: bool, meta: "dict | None") -> list[str]:
    """A fixed-width (CARD_W) plain card box. Critical-path cards get a loud
    double-line border (╔═╗ ║ ╚═╝); everything else a quiet single line."""
    if card.on_critical_path:
        tl, h, tr, v, bl, br = "╔", "═", "╗", "║", "╚", "╝"
    else:
        tl, h, tr, v, bl, br = "┌", "─", "┐", "│", "└", "┘"
    glyph = _GLYPH.get(card.kind, "·")
    cursor = "▸" if selected else " "
    lines = [tl + h * _INNER + tr]
    lines.append(v + _fit(f"{cursor}{glyph} {card.title}", _INNER) + v)
    lines.append(v + _fit(f"  {_state_text(card, meta)}", _INNER) + v)
    if card.tasks_total is not None:
        done = card.tasks_done or 0
        lines.append(v + _fit(f"  {done}/{card.tasks_total} tasks", _INNER) + v)
    lines.append(bl + h * _INNER + br)
    return lines


def _member_lines(members: list) -> list[str]:
    """Selected topic's member tasks, expanded in place under its card."""
    out = []
    for m in members or []:
        mark = "✓" if m.get("state") == "verified" else "·"
        out.append(_fit(f"  {mark} {m.get('title', m.get('id', '?'))}"))
    return out


def _anchor_column(anchor_count: int) -> list[str]:
    return [
        _fit("ANCHOR"),
        "┌" + "─" * _INNER + "┐",
        "│" + _fit(f" ▦ {anchor_count} done", _INNER) + "│",
        "│" + _fit("   verified", _INNER) + "│",
        "└" + "─" * _INNER + "┘",
    ]


def _wave_header(wave) -> str:
    return _fit(f"WAVE {wave.index + 1}")


def _wave_column(wave, selected_id, meta_by_id, member_tasks) -> list[str]:
    lines = [_wave_header(wave)]
    for card in wave.cards:
        sel = card.id == selected_id
        lines += _card_lines(card, selected=sel, meta=(meta_by_id or {}).get(card.id))
        if sel:
            lines += _member_lines((member_tasks or {}).get(card.id, []))
        lines.append(" " * CARD_W)  # inter-card spacer
    return lines


def _zip_columns(columns: list[list[str]]) -> list[str]:
    """Join columns side-by-side. Every column is padded to the tallest one's
    height (bounds-safety) and every cell to CARD_W before joining."""
    if not columns:
        return []
    height = max(len(c) for c in columns)
    rows = []
    for i in range(height):
        cells = [(c[i] if i < len(c) else " " * CARD_W) for c in columns]
        cells = [(cell if len(cell) >= CARD_W else cell.ljust(CARD_W)) for cell in cells]
        rows.append(COL_GAP.join(cells).rstrip())
    return rows


def visible_wave_span(n_waves: int, pan: int, width: "int | None") -> tuple[int, int]:
    """(start, count) of wave columns to show: the anchor is always pinned, the
    rest of the terminal width pages over waves from ``pan``. Clamped so pan
    never scrolls past the last wave."""
    if width and width > 0:
        fit = max(1, width // _STRIDE)
    else:
        fit = n_waves + 1
    wave_slots = max(1, fit - 1)  # one slot reserved for the pinned anchor
    start = max(0, min(pan, max(0, n_waves - 1)))
    return start, wave_slots


def plan_grid_rows(
    layout, *, selected_id=None, meta_by_id=None, member_tasks=None,
    pan=0, width=None,
) -> tuple[list[str], int, int]:
    """The width-critical part: the anchor-pinned, paged wave-column grid as
    plain fixed-width rows. Returns (rows, shown_wave_count, total_wave_count).
    Every row is ≤ ``width`` because the anchor plus only as many wave columns
    as fit are shown (the rest page in via ``pan``) — the narrow-viewport
    degradation contract the smoke matrix pins."""
    if not layout.waves:
        return [], 0, 0
    columns = [_anchor_column(layout.anchor_count)]
    for wave in layout.waves:
        columns.append(_wave_column(wave, selected_id, meta_by_id, member_tasks))
    start, wave_slots = visible_wave_span(len(layout.waves), pan, width)
    wave_cols = columns[1:]
    shown = wave_cols[start:start + wave_slots]
    grid = _zip_columns([columns[0]] + shown)
    return grid, len(shown), len(layout.waves)


def render_plan_body(
    layout, *, selected_id=None, meta_by_id=None, member_tasks=None,
    titles=None, pan=0, width=None, project_header="Plan",
) -> list[str]:
    """Full Plan-view body: project header, runnable-now capacity readout, the
    zipped wave-column grid (anchor pinned + paged waves), fold summary, and
    the critical-path + legend footers."""
    titles = titles or {}
    header = style(f"◈ Plan — {project_header}", None, bold=True)
    if not layout.waves:
        done = style(f"▦ anchor · {layout.anchor_count} verified — nothing open", DIM)
        return [header, "", done]

    grid, shown_waves, total_waves = plan_grid_rows(
        layout, selected_id=selected_id, meta_by_id=meta_by_id,
        member_tasks=member_tasks, pan=pan, width=width,
    )
    start, _ = visible_wave_span(total_waves, pan, width)

    w0 = layout.waves[0]
    cap = f"WAVE 1 runnable now — {w0.parallel_count} parallel"
    if w0.free_slots is not None:
        cap += f" · {w0.free_slots} slots free"
        if w0.queued:
            cap += f" → {w0.queued} queue"
    lines = [header, style(cap, GREEN), ""]
    if shown_waves < total_waves:
        lines.append(style(
            f"←/→ pan · showing waves {start + 1}–{start + shown_waves}"
            f" of {total_waves}", DIM,
        ))
    lines += grid
    lines.append("")
    if layout.fold_summary:
        lines.append(style(layout.fold_summary, DIM))
    lines.append(_critical_footer(layout, titles))
    return lines


def render_plan_legend() -> str:
    """The one-line key legend, rendered separately so the screen can DOCK it at
    the bottom — keeping it in the terminal's bottom band on any viewport height
    (a top-anchored scroll body would otherwise scroll it off / leave it mid
    screen, failing the smoke chrome check)."""
    return _legend_footer()


def _critical_footer(layout, titles) -> str:
    if not layout.critical_path:
        return style("critical path — none (nothing open)", DIM)
    names = " ▸ ".join(titles.get(nid, nid) for nid in layout.critical_path)
    n_waves = len({c.wave for w in layout.waves for c in w.cards
                   if c.id in set(layout.critical_path)})
    return (
        style("critical path ║ ", AMBER) + style(names, AMBER)
        + style(f" — {n_waves} waves to done", DIM)
    )


def _legend_footer() -> str:
    legend = (
        f"{style('◐', AMBER)} running  {style('◇', GREEN)} ready  "
        f"{style('○', DIM)} blocked  {style('✗', RED)} failed"
    )
    keys = "j/k select · enter open · z fold · ←/→ pan · L railroad · q/esc close"
    return legend + style(f" · {keys}", DIM)
