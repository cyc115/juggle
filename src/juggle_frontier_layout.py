"""juggle_frontier_layout — pure row/rail layout engine for the Frontier
Railroad (fr-layout, 2026-07-02). Turns a project snapshot (topics w/ states
+ task counts, topic dependency edges, free agent slots) into a deterministic
TOP-DOWN row list: a trunk anchor stub for verified history, then open
topics one row per topic in topological order (reading order = execution
order), with dotted "wave N" band rows inserted wherever dependency depth
increments.

Wave layering + critical-path math adapted from the parked cyc_CR:530bee5
Plan-view engine (juggle_plan_layout) — longest-path ranks over the OPEN
subgraph, critical path = longest unfinished chain walked back from the
deepest rank. Lane packing for the forward-dependency rail gutter reuses the
existing juggle_cockpit_graph_lanes.assign_lanes core (Surface-D/B, 2026-06-30
graph railroad) rather than re-deriving lane math.

No Textual, no DB, no Rich — fully unit-testable pure functions over plain
dataclasses. Edges are (task_id, depends_on_id) pairs, same convention as
juggle_cockpit_graph_layout (arrow points at the dependency).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from juggle_cockpit_graph_layout import GraphTask, assign_ranks, build_ranks
from juggle_cockpit_graph_lanes import assign_lanes

IN_FLIGHT_STATES = ("dispatching", "running", "integrating", "integrated-unlanded")
FAILED_STATES = ("failed-exec", "failed-integration", "failed-verify", "blocked-failed")


@dataclass(frozen=True)
class FrontierRow:
    """One railroad row. ``kind`` is "running" | "ready" | "blocked" | "failed"
    for a topic row, or "wave-band" / "fold-summary" for a separator row."""
    id: str
    title: str
    state: str
    kind: str
    wave: int
    lane: int = 0
    tasks_done: "int | None" = None
    tasks_total: "int | None" = None
    on_critical_path: bool = False
    blocked_on: tuple[str, ...] = field(default_factory=tuple)
    parallel_count: "int | None" = None  # set on wave-band / fold-summary rows
    free_slots: "int | None" = None      # wave-band, wave 0 only
    queued: "int | None" = None          # wave-band, wave 0 only


@dataclass(frozen=True)
class FrontierLayout:
    anchor_count: int
    rows: tuple[FrontierRow, ...] = ()
    critical_path: tuple[str, ...] = ()
    critical_path_lanes: tuple[int, ...] = ()
    lane_count: int = 0


def _row_kind(state: str) -> str:
    if state in IN_FLIGHT_STATES:
        return "running"
    if state in FAILED_STATES:
        return "failed"
    return "ready"  # wave/blocked distinction is applied by the caller


def _critical_path(rank_of: dict[str, int], deps_of: dict[str, list[str]]) -> tuple[str, ...]:
    """Longest chain of unfinished topics: walk back from the deepest rank,
    always choosing a dependency exactly one rank shallower (tie-break by id).
    Identical math to juggle_plan_layout._critical_path (cyc_CR:530bee5)."""
    if not rank_of:
        return ()
    max_rank = max(rank_of.values())
    node = min(tid for tid, r in rank_of.items() if r == max_rank)
    path = [node]
    while rank_of[node] > 0:
        target = rank_of[node] - 1
        preds = sorted(d for d in deps_of.get(node, ()) if rank_of.get(d) == target)
        if not preds:
            break
        node = preds[0]
        path.append(node)
    path.reverse()
    return tuple(path)


def build_frontier_layout(
    tasks: list[GraphTask],
    edges: list[tuple[str, str]],
    free_slots: int,
    *,
    folded_waves: "set[int] | None" = None,
) -> FrontierLayout:
    """Build the deterministic Frontier Railroad row list for one project
    snapshot. ``folded_waves`` (owned by the caller — the screen toggles this
    on 'z') collapses each named wave index to one fold-summary row."""
    anchor_count = sum(1 for t in tasks if t.state == "verified")
    open_tasks = [t for t in tasks if t.state != "verified"]
    if not open_tasks:
        return FrontierLayout(anchor_count=anchor_count)

    open_ids = {t.id for t in open_tasks}
    deps_of: dict[str, list[str]] = {}
    for task_id, dep_id in edges:
        if task_id in open_ids and dep_id in open_ids:
            deps_of.setdefault(task_id, []).append(dep_id)
    titles = {t.id: t.title for t in open_tasks}

    rank_of = assign_ranks(open_tasks, edges)
    ranks = build_ranks(open_tasks, edges)
    lane_layout = assign_lanes(open_tasks, edges)
    lane_of = {n.id: n.lane for n in lane_layout.nodes}

    critical_path = _critical_path(rank_of, deps_of)
    critical_set = set(critical_path)
    critical_path_lanes = tuple(lane_of[nid] for nid in critical_path)

    folded = folded_waves or set()
    rows: list[FrontierRow] = []
    for i, rank in enumerate(ranks):
        cells = sorted(rank.tasks, key=lambda t: t.id)
        # Separator band at each depth increment (design point 3). The FIRST
        # such band (entering wave 1) doubles as wave 0's dispatch-capacity
        # readout ("N can start in parallel, M slots free -> K queues") since
        # wave 0 is what's actually actionable right now.
        if i > 0:
            band_free_slots = band_queued = None
            if i == 1:
                prev_count = len(ranks[0].tasks)
                band_free_slots = free_slots
                band_queued = max(0, prev_count - free_slots)
            rows.append(FrontierRow(
                id=f"__wave_{rank.index}__", title="", state="",
                kind="wave-band", wave=rank.index, parallel_count=len(cells),
                free_slots=band_free_slots, queued=band_queued,
            ))
        if rank.index in folded:
            rows.append(FrontierRow(
                id=f"__fold_wave_{rank.index}__", title="", state="",
                kind="fold-summary", wave=rank.index, parallel_count=len(cells),
            ))
            continue
        for t in cells:
            kind = _row_kind(t.state)
            if kind == "ready" and rank.index > 0:
                kind = "blocked"
            blocked_on = ()
            if kind == "blocked":
                blocked_on = tuple(sorted(
                    titles[d] for d in deps_of.get(t.id, ()) if d in titles
                ))
            rows.append(FrontierRow(
                id=t.id, title=t.title, state=t.state, kind=kind,
                wave=rank.index, lane=lane_of.get(t.id, 0),
                tasks_done=t.tasks_done, tasks_total=t.tasks_total,
                on_critical_path=t.id in critical_set,
                blocked_on=blocked_on,
            ))

    if len(ranks) == 1:
        # No depth increment ever fires — still surface wave 0's own
        # dispatch-capacity readout so free-slot info is never silently lost.
        count = len(ranks[0].tasks)
        rows.append(FrontierRow(
            id="__wave_0__", title="", state="", kind="wave-band",
            wave=0, parallel_count=count,
            free_slots=free_slots, queued=max(0, count - free_slots),
        ))

    return FrontierLayout(
        anchor_count=anchor_count,
        rows=tuple(rows),
        critical_path=critical_path,
        critical_path_lanes=critical_path_lanes,
        lane_count=lane_layout.lane_count,
    )
