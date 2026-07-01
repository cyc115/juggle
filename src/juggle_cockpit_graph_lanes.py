"""juggle_cockpit_graph_lanes — PURE git-graph lane packing over a task DAG
(2026-06-30 graph railroad). No DB, no Rich, no Textual. Shared by the Surface-D
spine and the Surface-B railroad Screen."""
from __future__ import annotations

from dataclasses import dataclass

from juggle_cockpit_graph_layout import build_ranks


@dataclass(frozen=True)
class LaneNode:
    id: str
    row: int
    lane: int
    state: str
    fan_out: int
    fan_in: int


@dataclass(frozen=True)
class LaneLayout:
    nodes: list[LaneNode]
    lane_count: int


def _leftmost_free(lanes: list) -> int:
    for i, h in enumerate(lanes):
        if h is None:
            return i
    lanes.append(None)
    return len(lanes) - 1


def assign_lanes(tasks, edges) -> LaneLayout:
    order = [n for rank in build_ranks(tasks, edges) for n in rank.tasks]
    ids = {n.id for n in order}
    row_of = {n.id: i for i, n in enumerate(order)}
    state = {n.id: n.state for n in order}
    dependents: dict[str, list[str]] = {n.id: [] for n in order}
    for node_id, dep_id in edges:
        if node_id in ids and dep_id in ids:
            dependents[dep_id].append(node_id)
    for k in dependents:
        dependents[k].sort(key=lambda v: (row_of[v], v))

    lanes: list = []  # per column: id this lane is heading toward, or None
    out: list[LaneNode] = []
    for row, n in enumerate(order):
        nid = n.id
        heading = [i for i, h in enumerate(lanes) if h == nid]
        if heading:
            lane = heading[0]
            fan_in = len(heading)
            for i in heading[1:]:
                lanes[i] = None
        else:
            lane = _leftmost_free(lanes)
            fan_in = 0
        lanes[lane] = None
        deps_v = dependents[nid]
        if deps_v:
            lanes[lane] = deps_v[0]
            for v in deps_v[1:]:
                lanes[_leftmost_free(lanes)] = v
        out.append(LaneNode(nid, row, lane, state[nid], len(deps_v), fan_in))
    lane_count = max((ln.lane for ln in out), default=-1) + 1
    return LaneLayout(out, lane_count)
