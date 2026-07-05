"""Ordering + snapshot cache for the cockpit graph panel.

Two responsibilities, both pure/testable:

1. ``order_projects`` — the ordering MODEL. Two partitions, ACTIVE always above
   DONE (INBOX pinned first). ACTIVE = a project with >=1 open (non-verified)
   root topic/task, sorted by live agent-activity DESC (tiebreak id). DONE = zero
   open work, sunk below all active projects, sorted by completion recency DESC.

2. The snapshot THROTTLE. A caller-owned ``OrderCache`` freezes the computed
   order for ``interval`` seconds so the panel does not reshuffle on every render.
   Within the window the frozen order is returned verbatim — since-deleted
   projects are dropped and brand-new ones are appended at the TAIL (so a new
   project appears without reshuffling the rest). ``now`` is injected so tests
   never sleep; ``interval<=0`` bypasses the cache (recompute every call).

The DB-reading side (``gather_project_activity``) lives in
juggle_cockpit_graph_activity so this module stays pure — no sqlite, no Rich, no time.
"""
from __future__ import annotations

from dataclasses import dataclass

INBOX_ID = "INBOX"


@dataclass(frozen=True)
class ProjectActivity:
    """One project's ordering inputs (pure — gathered from the DB elsewhere).

    ``active_key`` / ``done_key`` are opaque comparable sort keys (ISO-timestamp
    strings in production, ints in tests); DESC with an id tiebreak.
    """

    id: str
    is_done: bool
    active_key: object = ""
    done_key: object = ""


@dataclass
class OrderCache:
    """Mutable snapshot cell: the frozen order + the monotonic ts it was taken."""

    ordered: "list[str] | None" = None
    ts: "float | None" = None


# Module-level default cache used by the live cockpit call site. Tests inject
# their own OrderCache so they never touch this shared cell.
_DEFAULT_CACHE = OrderCache()


def _fresh_order(rows: "list[ProjectActivity]") -> list[str]:
    """Recompute the full partitioned order from scratch.

    INBOX first (if present), then ACTIVE (agent-activity DESC), then DONE
    (completion-recency DESC). Ties broken by id ASC via a stable pre-sort.
    """
    inbox = [r for r in rows if r.id == INBOX_ID]
    rest = [r for r in rows if r.id != INBOX_ID]

    active = [r for r in rest if not r.is_done]
    done = [r for r in rest if r.is_done]

    # Stable sort trick: id ASC first, then the primary key DESC — equal primary
    # keys keep the id-ascending order.
    active.sort(key=lambda r: r.id)
    active.sort(key=lambda r: r.active_key, reverse=True)
    done.sort(key=lambda r: r.id)
    done.sort(key=lambda r: r.done_key, reverse=True)

    return [r.id for r in inbox] + [r.id for r in active] + [r.id for r in done]


def order_projects(
    rows: "list[ProjectActivity]",
    *,
    now: float,
    interval: float,
    cache: "OrderCache | None" = None,
) -> list[str]:
    """Return the ordered project-id list, honoring the snapshot throttle.

    Within ``interval`` of the last snapshot the frozen order is reused (deleted
    projects dropped, new ones appended at the tail). Otherwise the order is
    recomputed and the snapshot reset to ``now``. ``interval<=0`` always
    recomputes. ``now`` is the injected clock (time.monotonic() at the call
    site).
    """
    cache = cache if cache is not None else _DEFAULT_CACHE

    fresh_within_window = (
        interval > 0
        and cache.ordered is not None
        and cache.ts is not None
        and (now - cache.ts) < interval
    )
    if fresh_within_window:
        current = {r.id for r in rows}
        frozen = [pid for pid in cache.ordered if pid in current]
        seen = set(frozen)
        appended = [r.id for r in rows if r.id not in seen]
        return frozen + appended

    ordered = _fresh_order(rows)
    cache.ordered = list(ordered)
    cache.ts = now
    return ordered
