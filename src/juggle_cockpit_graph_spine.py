"""juggle_cockpit_graph_spine — Surface-D inline dependency spine (2026-06-30
graph railroad). Renders a LaneLayout as a compact single-line git-graph: dots
(one per task, colored by state) joined by fan-aware connectors (─ linear, ┬
fan-out, ┴ fan-in), a trailing N/M verified count and ◇/✗ blocked/failed
segments. Wide fans collapse to a ⧉k stacked marker; the assembled plain text
never exceeds ``width`` (single-cell glyphs from the legend guarantee display ==
len). Pure — Rich Text only, no DB, no Textual."""
from __future__ import annotations

from rich.text import Text
from rich.style import Style

from juggle_cockpit_legend import railroad_glyph

# Reuse the panel's per-state colours (single source, via the extracted rows mod).
from juggle_cockpit_graph_rows import _STATE_COLORS

_FAILED = ("failed-exec", "failed-integration", "failed-verify")


def _connector(left, right) -> str:
    """The join char between two adjacent dots: branch on a fan-out, merge on a
    fan-in, else a plain rail."""
    if left.fan_out > 1:
        return "┬"
    if right.fan_in > 1:
        return "┴"
    return "─"


def _suffix(nodes) -> str:
    total = len(nodes)
    verified = sum(1 for n in nodes if n.state == "verified")
    blocked = sum(1 for n in nodes if n.state == "blocked-failed")
    failed = sum(1 for n in nodes if n.state in _FAILED)
    s = f" {verified}/{total}"
    if blocked:
        s += f" ◇{blocked}"
    if failed:
        s += f" ✗{failed}"
    return s


def _segments(layout, lane_cap: int) -> list[tuple[str, str | None]]:
    """(text, colour) segments for the spine body, in row order. Dots carry a
    state colour; connectors / the ⧉ stacked marker carry None. A wide fan
    (lane_count > lane_cap) collapses the fanning node's children into ⧉k."""
    nodes = sorted(layout.nodes, key=lambda n: n.row)
    compress = layout.lane_count > lane_cap
    segs: list[tuple[str, str | None]] = []
    i = 0
    n = len(nodes)
    while i < n:
        node = nodes[i]
        segs.append((railroad_glyph(node.state), _STATE_COLORS.get(node.state)))
        if compress and node.fan_out > 1:
            segs.append((f"┬⧉{node.fan_out}", None))
            i += 1 + node.fan_out          # skip the collapsed parallel fan
            if i < n:
                segs.append(("─", None))   # reconnect to the continuation
            continue
        i += 1
        if i < n:
            segs.append((_connector(node, nodes[i]), None))
    return segs


def _fit(segs, suffix: str, width: int) -> list[tuple[str, str | None]]:
    """Trim segments so ``len(body) + len(suffix) <= width``; append a … marker
    when anything was dropped."""
    avail = max(1, width - len(suffix))
    kept: list[tuple[str, str | None]] = []
    used = 0
    for seg in segs:
        if used + len(seg[0]) > avail:
            break
        kept.append(seg)
        used += len(seg[0])
    if len(kept) < len(segs):
        while kept and used + 1 > avail:
            used -= len(kept[-1][0])
            kept.pop()
        kept.append(("…", None))
    return kept


def spine_plain(layout, *, width: int, lane_cap: int = 6, count: bool = True) -> str:
    """Plain-text (no markup) spine — for exact-assert tests and width checks.

    ``count=False`` drops the trailing N/M suffix (the header keeps its own
    format_progress count, so the spine owns only the dependency shape)."""
    suffix = _suffix(layout.nodes) if count else ""
    kept = _fit(_segments(layout, lane_cap), suffix, width)
    return ("".join(t for t, _ in kept) + suffix)[:width]


def render_spine(layout, *, width: int, lane_cap: int = 6, count: bool = True) -> Text:
    """Rich Text spine — dots coloured by state, connectors/suffix plain."""
    suffix = _suffix(layout.nodes) if count else ""
    kept = _fit(_segments(layout, lane_cap), suffix, width)
    out = Text(no_wrap=True, overflow="ellipsis")
    for text, colour in kept:
        out.append(text, style=Style(color=colour) if colour else None)
    if suffix:
        out.append(suffix, style=Style(dim=True))
    return out
