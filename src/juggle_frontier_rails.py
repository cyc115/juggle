"""juggle_frontier_rails -- pure forward-dependency rail tracer for the
Frontier Railroad (fr-vf-rails, 2026-07-03). Traces open-subgraph dependency
edges (dependent, dependency) into per-row lane gutter cells: a vertical pass
in the source's lane, an elbow (corner + horizontal + arrow) landing at the
dependent's own lane, amber double-line when both ends are on the critical
path. Also the single source of the Rich color tokens the Frontier Railroad
renderer draws from (juggle_frontier_render) -- CLAUDE.md: one source of
truth. No Textual, no DB, no Rich import -- markup is emitted as plain
``[style]...[/]`` strings the caller feeds to a markup=True Static."""
from __future__ import annotations

from dataclasses import dataclass

AMBER = "dark_orange"
GREEN = "green3"
DIM = "grey50"
RED = "red3"

_NODE_KINDS = ("running", "ready", "blocked", "failed")


@dataclass(frozen=True)
class RailCell:
    char: str
    color: "str | None" = None

    def render(self) -> str:
        if self.color is None:
            return self.char
        return f"[{self.color}]{self.char}[/]"


def _leftmost_free(lanes: list) -> int:
    for i, h in enumerate(lanes):
        if h is None:
            return i
    lanes.append(None)
    return len(lanes) - 1


def _replay_edge_lanes(order_ids: list[str], deps_of: "dict[str, list[str]]"):
    """Replay assign_lanes' leftmost-free packing (juggle_cockpit_graph_lanes)
    to find the lane each dependency->dependent edge travels in, so gutter
    columns line up with FrontierRow.lane. The first (row-order) dependent of
    a node inherits that node's own lane; later dependents branch to a fresh
    lane -- identical math to assign_lanes, replayed here because that module
    only exposes the final per-node lane, not per-edge lane."""
    dependents: dict[str, list[str]] = {nid: [] for nid in order_ids}
    for dependent, deps in deps_of.items():
        for dep in deps:
            if dep in dependents:
                dependents[dep].append(dependent)
    row_of = {nid: i for i, nid in enumerate(order_ids)}
    for k in dependents:
        dependents[k].sort(key=lambda v: (row_of.get(v, 0), v))

    lanes: list = []
    edge_lane: dict[tuple[str, str], int] = {}
    for nid in order_ids:
        heading = [i for i, h in enumerate(lanes) if h == nid]
        if heading:
            lane = heading[0]
            for i in heading[1:]:
                lanes[i] = None
        else:
            lane = _leftmost_free(lanes)
        lanes[lane] = None
        deps_v = dependents.get(nid, [])
        if deps_v:
            lanes[lane] = deps_v[0]
            edge_lane[(nid, deps_v[0])] = lane
            for v in deps_v[1:]:
                fl = _leftmost_free(lanes)
                lanes[fl] = v
                edge_lane[(nid, v)] = fl
    return edge_lane


def source_color(state: str, on_critical: bool) -> str:
    """The Rich style a rail segment (or dep name) takes from its source
    node: AMBER on the critical path, GREEN when ready/verified, DIM
    otherwise."""
    if on_critical:
        return AMBER
    if state in ("ready", "verified"):
        return GREEN
    return DIM


def build_rail_gutters(layout, edges: list[tuple[str, str]]) -> list[list[RailCell]]:
    """Per-row lane gutters for every row in ``layout.rows`` (node rows AND
    wave-band/fold-summary rows -- bands get pass-through-only verticals for
    any edge spanning across them, never a corner). ``edges`` is
    (dependent_id, dependency_id) pairs, the same convention
    build_frontier_layout takes. Bounds-safety: carries forward the
    2026-07-02 gitlog lane IndexError incident (a lane index computed for an
    edge must never be indexed past ``layout.lane_count`` -- out-of-bounds
    edges are dropped, never raised)."""
    lane_count = max(layout.lane_count, 1)
    n_rows = len(layout.rows)
    grid: list[list["RailCell | None"]] = [[None] * lane_count for _ in range(n_rows)]
    if n_rows == 0:
        return []

    node_rows = [r for r in layout.rows if r.kind in _NODE_KINDS]
    order_ids = [r.id for r in node_rows]
    row_of = {r.id: i for i, r in enumerate(layout.rows) if r.kind in _NODE_KINDS}
    state_of = {r.id: r.state for r in node_rows}
    own_lane_of = {r.id: r.lane for r in node_rows}
    critical_set = set(layout.critical_path)

    deps_of: dict[str, list[str]] = {}
    for dependent, dependency in edges:
        if dependent in row_of and dependency in row_of:
            deps_of.setdefault(dependent, []).append(dependency)

    edge_lane = _replay_edge_lanes(order_ids, deps_of)

    traced: list[tuple[str, str, int, int, int, str]] = []
    for dependent, deps in deps_of.items():
        for dependency in deps:
            lane = edge_lane.get((dependency, dependent))
            if lane is None or lane >= lane_count:
                continue
            src_row, dst_row = row_of[dependency], row_of[dependent]
            if src_row >= dst_row:
                continue
            on_cp = dependency in critical_set and dependent in critical_set
            color = source_color(state_of[dependency], on_cp)
            traced.append((dependency, dependent, src_row, dst_row, lane, color))

    for _, _, src_row, dst_row, lane, color in traced:
        vert = "║" if color == AMBER else "│"
        for r in range(src_row + 1, dst_row):
            grid[r][lane] = RailCell(vert, color)

    by_dst: dict[int, list] = {}
    for edge in traced:
        by_dst.setdefault(edge[3], []).append(edge)
    for dst_row, group in by_dst.items():
        dependent = group[0][1]
        v_lane = min(own_lane_of.get(dependent, 0), lane_count - 1)
        span_colors: dict[int, str] = {}
        for _, _, _, _, lane, color in group:
            span_colors[lane] = color
            if lane == v_lane:
                grid[dst_row][lane] = RailCell("▸", color)
            else:
                corner = "╚" if color == AMBER else "└"
                grid[dst_row][lane] = RailCell(corner, color)
        lanes = list(span_colors) + [v_lane]
        lo, hi = min(lanes), max(lanes)
        fill_color = span_colors.get(hi, span_colors[min(span_colors)])
        for c in range(lo + 1, hi):
            if grid[dst_row][c] is None:
                grid[dst_row][c] = RailCell("─", fill_color)
        if v_lane not in span_colors and grid[dst_row][v_lane] is None:
            grid[dst_row][v_lane] = RailCell("▸", fill_color)

    return [[cell or RailCell(" ") for cell in row] for row in grid]


def gutter_string(cells: list[RailCell]) -> str:
    return "".join(c.render() for c in cells)


def gutter_plain(cells: list[RailCell]) -> str:
    return "".join(c.char for c in cells)
