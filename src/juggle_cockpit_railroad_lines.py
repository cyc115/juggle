"""juggle_cockpit_railroad_lines — PURE vertical railroad line-model (2026-06-30
graph railroad, T4). Turns a LaneLayout into one RailLine per task: a left
lane-column ``rail`` string (│ pass-through, ├/┬ fan-out branch, ┴/┤ fan-in
merge) plus the node's single-cell state ``glyph`` (from the legend). No Textual,
no DB, no Rich — the Surface-B Screen is a thin shell over this. The lane
occupancy is replayed with the SAME leftmost-free packing assign_lanes uses, so
``rail`` lines up with the lane indices in the layout."""
from __future__ import annotations

from dataclasses import dataclass

from juggle_cockpit_legend import railroad_glyph


@dataclass(frozen=True)
class RailLine:
    row: int
    rail: str
    glyph: str
    id: str
    title: str
    selected: bool


def _leftmost_free(lanes: list) -> int:
    for i, h in enumerate(lanes):
        if h is None:
            return i
    lanes.append(None)
    return len(lanes) - 1


def _replay(order, edges):
    """Replay assign_lanes' packing to capture, per row, the lane occupancy and
    the heading (merged) / branched (fanned) lane indices. Returns a list of
    dicts row-aligned with ``order``."""
    ids = {n.id for n in order}
    row_of = {n.id: i for i, n in enumerate(order)}
    dependents: dict[str, list[str]] = {n.id: [] for n in order}
    for node_id, dep_id in edges:
        if node_id in ids and dep_id in ids:
            dependents[dep_id].append(node_id)
    for k in dependents:
        dependents[k].sort(key=lambda v: (row_of[v], v))

    lanes: list = []
    rows: list[dict] = []
    for n in order:
        nid = n.id
        before = [i for i, h in enumerate(lanes) if h is not None]
        heading = [i for i, h in enumerate(lanes) if h == nid]
        if heading:
            lane = heading[0]
            for i in heading[1:]:
                lanes[i] = None
        else:
            lane = _leftmost_free(lanes)
        lanes[lane] = None
        deps_v = dependents[nid]
        branched: list[int] = []
        if deps_v:
            lanes[lane] = deps_v[0]
            for v in deps_v[1:]:
                fl = _leftmost_free(lanes)
                lanes[fl] = v
                branched.append(fl)
        after = [i for i, h in enumerate(lanes) if h is not None]
        rows.append({
            "lane": lane, "heading": heading, "branched": branched,
            "before": before, "after": after,
        })
    return rows


def _rail_str(info: dict, width: int) -> str:
    """Build the lane-gutter string for one row from its occupancy info."""
    lane = info["lane"]
    cols = [" "] * width
    for i in set(info["before"]) | set(info["after"]):
        if 0 <= i < width and i != lane:
            cols[i] = "│"
    branched = info["branched"]
    heading = info["heading"]
    if branched:  # fan-out — branch right toward the new lanes
        cols[lane] = "├"
        span = [lane] + branched
        for c in range(lane + 1, max(span)):
            if cols[c] == " ":
                cols[c] = "─"
        for b in branched:
            cols[b] = "┬"
    elif len(heading) > 1:  # fan-in — merge the incoming lanes
        cols[lane] = "┴"
        span = heading
        for c in range(lane + 1, max(span)):
            if cols[c] == " ":
                cols[c] = "─"
        for h in heading:
            if h != lane:
                cols[h] = "┴"
    else:
        cols[lane] = "│"
    return "".join(cols)


def railroad_lines(layout, tasks, *, selected_row: int) -> list[RailLine]:
    """One RailLine per task, in row (topo) order. ``rail`` is the lane gutter;
    ``glyph`` is the node's single-cell state marker (placed by the Screen to the
    right of the gutter)."""
    order = sorted(layout.nodes, key=lambda n: n.row)
    titles = {t.id: t.title for t in tasks}
    width = max(layout.lane_count, 1)
    infos = _replay(order, getattr(layout, "edges", ()) or ())
    out: list[RailLine] = []
    for node, info in zip(order, infos):
        out.append(RailLine(
            row=node.row,
            rail=_rail_str(info, width),
            glyph=railroad_glyph(node.state),
            id=node.id,
            title=titles.get(node.id, node.id),
            selected=(node.row == selected_row),
        ))
    return out
