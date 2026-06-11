"""Pure task-graph layout engine for the cockpit graph panel.

Turns (nodes, edges) into a left-to-right layered DAG:
  - rank assignment via longest dependency depth (longest-path layering),
  - ordered cells per rank,
  - narrow-width collapse (focus+context: fold verified / far-pending ranks,
    keep ready/running/failed expanded),
  - horizontal pan windowing with a minimap descriptor.

No Textual, no DB, no Rich — fully unit-testable pure functions over plain
dataclasses. Edges are (node_id, depends_on_id) pairs (arrow points at the
dependency; the graph flows dependency → dependent, rendered left → right).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# States that must stay visible (the operator's focus) when narrow.
_ACTIVE_PREFIXES = ("ready", "running", "dispatching", "integrating", "failed", "blocked")
# States safe to fold to a count under width pressure (context only).
_FOLDABLE = ("verified", "pending")


@dataclass(frozen=True)
class GraphNode:
    id: str
    title: str
    state: str
    thread_id: str | None = None
    user_label: str | None = None
    tasks_done: "int | None" = None
    tasks_total: "int | None" = None


@dataclass
class Rank:
    index: int
    nodes: list[GraphNode] = field(default_factory=list)
    collapsed: bool = False
    label: str = ""  # set when collapsed, e.g. "…12 verified"


@dataclass(frozen=True)
class Minimap:
    total: int
    first: int  # 0-based first visible rank index
    last: int   # 0-based last visible rank index


# ---------------------------------------------------------------------------
# Rank assignment
# ---------------------------------------------------------------------------


def assign_ranks(nodes: list[GraphNode], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Map node id -> rank, rank = longest dependency depth (longest path).

    Cycle-guarded: iterates to a fixpoint, capped at len(nodes) passes so a
    malformed cyclic input terminates with finite ranks instead of hanging.
    """
    ids = {n.id for n in nodes}
    deps: dict[str, list[str]] = {n.id: [] for n in nodes}
    for node_id, dep_id in edges:
        if node_id in ids and dep_id in ids:
            deps[node_id].append(dep_id)

    rank: dict[str, int] = {nid: 0 for nid in ids}
    # Longest-path relaxation: rank(n) = 1 + max(rank(dep)). Repeat until stable
    # or capped (cycle guard).
    for _ in range(len(nodes) + 1):
        changed = False
        for nid in ids:
            if deps[nid]:
                new_rank = 1 + max(rank[d] for d in deps[nid])
                if new_rank > rank[nid]:
                    rank[nid] = new_rank
                    changed = True
        if not changed:
            break
    return rank


def build_ranks(nodes: list[GraphNode], edges: list[tuple[str, str]]) -> list[Rank]:
    """Group nodes into ordered Rank columns (stable by node id within a rank)."""
    rank_of = assign_ranks(nodes, edges)
    by_rank: dict[int, list[GraphNode]] = {}
    for n in nodes:
        by_rank.setdefault(rank_of[n.id], []).append(n)
    out: list[Rank] = []
    for idx in sorted(by_rank):
        cells = sorted(by_rank[idx], key=lambda c: c.id)
        out.append(Rank(index=idx, nodes=cells))
    return out


# ---------------------------------------------------------------------------
# Collapse (focus + context)
# ---------------------------------------------------------------------------


def _rank_is_active(rank: Rank) -> bool:
    """A rank is 'active' (keep expanded) if any node is ready/running/failed."""
    return any(
        any(n.state.startswith(p) for p in _ACTIVE_PREFIXES) for n in rank.nodes
    )


def _fold(rank: Rank) -> Rank:
    """Return a collapsed copy of a rank with a '…N <state>' count label."""
    counts: dict[str, int] = {}
    for n in rank.nodes:
        counts[n.state] = counts.get(n.state, 0) + 1
    # Pick the dominant foldable state for the label.
    dominant = max(counts, key=lambda s: counts[s])
    label = f"…{len(rank.nodes)} {dominant}"
    return Rank(index=rank.index, nodes=list(rank.nodes), collapsed=True, label=label)


def collapse_ranks(ranks: list[Rank], max_visible_ranks: int) -> list[Rank]:
    """Fold foldable (verified/pending) ranks to counts until total <= budget.

    Active ranks (ready/running/failed) are never folded — focus+context. If
    folding every foldable rank still exceeds the budget, the foldable ranks are
    merged so the result length is hard-clamped to ``max_visible_ranks``.
    """
    if max_visible_ranks <= 0 or len(ranks) <= max_visible_ranks:
        return [Rank(index=r.index, nodes=list(r.nodes)) for r in ranks]

    # Fold every non-active rank whose nodes are all foldable.
    result: list[Rank] = []
    for r in ranks:
        foldable = all(n.state in _FOLDABLE for n in r.nodes) and not _rank_is_active(r)
        result.append(_fold(r) if foldable else Rank(index=r.index, nodes=list(r.nodes)))

    if len(result) <= max_visible_ranks:
        return result

    # Still too wide: keep active ranks (focus), and — when folds exist — reserve
    # one slot for a single merged fold-summary (context). Active ranks are
    # clamped to budget-1 so the summary always survives, preserving focus+context.
    active = [r for r in result if not r.collapsed]
    collapsed = [r for r in result if r.collapsed]
    if collapsed:
        keep_active = active[: max(0, max_visible_ranks - 1)]
        total = sum(len(r.nodes) for r in collapsed)
        # Dominant foldable state for an honest label (e.g. "…12 verified").
        counts: dict[str, int] = {}
        for r in collapsed:
            for n in r.nodes:
                counts[n.state] = counts.get(n.state, 0) + 1
        dominant = max(counts, key=lambda s: counts[s]) if counts else "folded"
        summary = Rank(
            index=collapsed[0].index,
            nodes=[n for r in collapsed for n in r.nodes],
            collapsed=True,
            label=f"…{total} {dominant}",
        )
        merged = [*keep_active, summary]
    else:
        merged = active[:max_visible_ranks]
    merged.sort(key=lambda r: r.index)
    return merged[:max_visible_ranks]


# ---------------------------------------------------------------------------
# Pan window + minimap
# ---------------------------------------------------------------------------


def pan_window(
    ranks: list[Rank], offset: int, visible_count: int
) -> tuple[list[Rank], Minimap]:
    """Return the ``visible_count`` ranks starting at ``offset`` + a Minimap.

    Offset is clamped so the window never runs off the right edge.
    """
    total = len(ranks)
    if total == 0:
        return [], Minimap(total=0, first=0, last=0)
    visible_count = max(1, min(visible_count, total))
    max_offset = total - visible_count
    offset = max(0, min(offset, max_offset))
    window = ranks[offset : offset + visible_count]
    return window, Minimap(total=total, first=offset, last=offset + len(window) - 1)


def minimap_bar(total: int, first: int, last: int) -> str:
    """Render a minimap bar '▁▁███▁ ranks 3–5/6' (1-based human label)."""
    if total <= 0:
        return ""
    cells = []
    for i in range(total):
        cells.append("█" if first <= i <= last else "▁")
    return f"{''.join(cells)} ranks {first + 1}–{last + 1}/{total}"
