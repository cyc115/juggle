"""juggle_plan_layout — pure wave-layout engine for the cockpit Plan view
(cockpit-plan-view, 2026-07-02). Turns a project snapshot (topic nodes + topic
dependency edges + free-agent-slot count) into deterministic dependency-wave
columns: verified work compresses to one anchor stub, open topics layer by
longest-path depth (reusing juggle_cockpit_graph_layout.assign_ranks/build_ranks
for rank assignment), ordered within each wave by barycenter to minimize edge
crossings, with the longest unfinished dependency chain flagged as the
critical path.

No Textual, no DB, no Rich — fully unit-testable pure functions over plain
dataclasses. Edges are (task_id, depends_on_id) pairs, same convention as
juggle_cockpit_graph_layout (arrow points at the dependency; task_id's wave is
always > depends_on_id's wave once both are in the open subgraph).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbops.terminal_states import COMPLETION_TERMINAL_STATES as DONE_STATES
from juggle_cockpit_graph_layout import GraphTask, assign_ranks, build_ranks

IN_FLIGHT_STATES = ("dispatching", "running", "integrating", "integrated-unlanded")
FAILED_STATES = ("failed-exec", "failed-integration", "failed-verify", "blocked-failed")
# COMPLETION terminals: excluded from the open/actionable waves just like
# verified history — a cancelled node is done, never an actionable card. Only
# 'verified' feeds the anchor count; 'cancelled' vanishes entirely (matches the
# railroad). DONE_STATES is now the SAME canonical
# dbops.terminal_states.COMPLETION_TERMINAL_STATES object shared with
# juggle_frontier_layout (Phase 0) — no more sync-by-comment.


@dataclass(frozen=True)
class PlanCard:
    id: str
    title: str
    state: str  # raw node state
    kind: str  # "running" | "ready" | "blocked" | "failed"
    wave: int
    tasks_done: "int | None" = None
    tasks_total: "int | None" = None
    on_critical_path: bool = False
    blocked_on: tuple[str, ...] = field(default_factory=tuple)  # titles of unmet direct deps


@dataclass(frozen=True)
class PlanWave:
    index: int
    cards: tuple[PlanCard, ...]
    parallel_count: int
    free_slots: "int | None" = None
    queued: "int | None" = None


@dataclass(frozen=True)
class PlanEdge:
    src: str
    dst: str
    from_wave: int
    to_wave: int
    on_critical_path: bool = False


@dataclass(frozen=True)
class PlanLayout:
    anchor_count: int
    waves: tuple[PlanWave, ...] = ()
    critical_path: tuple[str, ...] = ()
    edges: tuple[PlanEdge, ...] = ()
    fold_summary: "str | None" = None


def _card_kind(state: str, wave: int) -> str:
    if state in IN_FLIGHT_STATES:
        return "running"
    if state in FAILED_STATES:
        return "failed"
    return "ready" if wave == 0 else "blocked"


def _barycenter_order(ranks, deps_of: dict[str, list[str]]) -> dict[str, float]:
    """Position each task within its wave: wave 0 sorts by id; later waves sort
    by the mean position of their (already-ordered) dependencies, tie-broken
    by id. Deterministic left-to-right ordering that minimizes edge crossings."""
    order_pos: dict[str, float] = {}
    for rank in ranks:
        if rank.index == 0:
            ordered = sorted(rank.tasks, key=lambda t: t.id)
        else:
            def _key(t, deps_of=deps_of, order_pos=order_pos):
                preds = [order_pos[d] for d in deps_of.get(t.id, ()) if d in order_pos]
                bary = sum(preds) / len(preds) if preds else float("inf")
                return (bary, t.id)
            ordered = sorted(rank.tasks, key=_key)
        for i, t in enumerate(ordered):
            order_pos[t.id] = float(i)
    return order_pos


def _critical_path(rank_of: dict[str, int], deps_of: dict[str, list[str]]) -> tuple[str, ...]:
    """Longest chain of unfinished topics: walk back from the deepest rank,
    always choosing a dependency exactly one rank shallower (tie-break by id)."""
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


def _plan_edges(
    open_ids: set,
    edges: list[tuple[str, str]],
    rank_of: dict[str, int],
    cp_pairs: set[tuple[str, str]],
) -> tuple[PlanEdge, ...]:
    out = []
    for task_id, dep_id in edges:
        if task_id in open_ids and dep_id in open_ids:
            out.append(PlanEdge(
                src=task_id, dst=dep_id,
                from_wave=rank_of[dep_id], to_wave=rank_of[task_id],
                on_critical_path=(dep_id, task_id) in cp_pairs,
            ))
    out.sort(key=lambda e: (e.from_wave, e.dst, e.to_wave, e.src))
    return tuple(out)


def build_plan_layout(
    tasks: list[GraphTask],
    edges: list[tuple[str, str]],
    free_slots: int,
    *,
    max_visible_topics: "int | None" = None,
) -> PlanLayout:
    """Build the deterministic Plan-view layout for one project snapshot."""
    anchor_count = sum(1 for t in tasks if t.state == "verified")
    open_tasks = [t for t in tasks if t.state not in DONE_STATES]
    if not open_tasks:
        return PlanLayout(anchor_count=anchor_count)

    open_ids = {t.id for t in open_tasks}
    deps_of: dict[str, list[str]] = {}
    for task_id, dep_id in edges:
        if task_id in open_ids and dep_id in open_ids:
            deps_of.setdefault(task_id, []).append(dep_id)

    rank_of = assign_ranks(open_tasks, edges)
    ranks = build_ranks(open_tasks, edges)
    order_pos = _barycenter_order(ranks, deps_of)
    titles = {t.id: t.title for t in open_tasks}

    critical_path = _critical_path(rank_of, deps_of)
    critical_set = set(critical_path)
    cp_pairs = {(critical_path[i], critical_path[i + 1]) for i in range(len(critical_path) - 1)}

    waves: list[PlanWave] = []
    for rank in ranks:
        ordered = sorted(rank.tasks, key=lambda t: order_pos.get(t.id, 0.0))
        cards = []
        for t in ordered:
            blocked_on = ()
            if rank.index > 0:
                blocked_on = tuple(sorted(
                    titles[d] for d in deps_of.get(t.id, ()) if d in titles
                ))
            cards.append(PlanCard(
                id=t.id, title=t.title, state=t.state,
                kind=_card_kind(t.state, rank.index),
                wave=rank.index,
                tasks_done=t.tasks_done, tasks_total=t.tasks_total,
                on_critical_path=t.id in critical_set,
                blocked_on=blocked_on,
            ))
        parallel_count = len(cards)
        is_wave_zero = rank.index == 0
        waves.append(PlanWave(
            index=rank.index, cards=tuple(cards),
            parallel_count=parallel_count,
            free_slots=free_slots if is_wave_zero else None,
            queued=max(0, parallel_count - free_slots) if is_wave_zero else None,
        ))

    fold_summary = None
    if max_visible_topics is not None and len(open_tasks) > max_visible_topics:
        kept: list[PlanWave] = [waves[0]]
        kept_count = len(waves[0].cards)
        for w in waves[1:]:
            if kept_count + len(w.cards) <= max_visible_topics:
                kept.append(w)
                kept_count += len(w.cards)
            else:
                break
        dropped = len(open_tasks) - kept_count
        if dropped > 0:
            waves = kept
            noun = "node" if dropped == 1 else "nodes"
            fold_summary = f"…{dropped} more open {noun}, z expands"

    visible_ids = {c.id for w in waves for c in w.cards}
    plan_edges = _plan_edges(visible_ids, edges, rank_of, cp_pairs)

    return PlanLayout(
        anchor_count=anchor_count,
        waves=tuple(waves),
        critical_path=critical_path,
        edges=plan_edges,
        fold_summary=fold_summary,
    )
