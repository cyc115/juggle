"""juggle_graph_hydration_learnings — the gl-hydrate learnings helpers.

Split out of juggle_graph_hydration (LOC gate) — the pure render/order helpers
plus the one DB-reading aggregator behind topic-level learnings injection
(spec 2026-07-03-graph-node-learnings §4 + Final revisions). juggle_graph_hydration
re-exports these so existing importers (and juggle_graph_repair) keep working.

Handoff vs learnings — the HARD delineation (spec 🔴2 / Final revisions): the two
channels are structurally distinct node columns and must NEVER carry the same dep
content twice. handoff = successor-facing interface (what was built / how to use
it; completion-time, enforced by the topic gate); learnings = non-obvious gotcha /
constraint / decision-why (mid-task upsert, optional). Hydration sources each block
from its own column and never re-derives one from the other; the instruction line
states the contract to the agent so it does not double-write.
"""

from __future__ import annotations

_LEARNINGS_CAP = 10

_LEARNINGS_INSTRUCTION = (
    "## Learnings — pull more, record yours\n"
    "- Broader context, on your judgment: `juggle graph learnings --topic <topic-id>` "
    "or `--project <project-id>`.\n"
    "- Record a non-obvious learning before completing: "
    '`juggle graph learn <node-id> "<≤300 chars>"`.\n'
    "- Handoff vs learnings: a handoff is the successor-facing interface (what you built "
    "/ how to use it); a learning is a non-obvious gotcha, constraint, or decision-why. "
    "Never duplicate handoff content in a learning."
)


def _render_learnings_lines(items: list[dict], *, cap: int = _LEARNINGS_CAP) -> str | None:
    """Render learnings ``items`` (caller-ordered, most-recent-first) as
    ``- <node-id>: <text>`` lines, capped at ``cap`` with a no-silent-drop
    breadcrumb (repo norm) pointing at the queryable tail. None when empty."""
    if not items:
        return None
    shown = items[:cap]
    lines = [f"- {it['node_id']}: {(it.get('text') or '').strip()}" for it in shown]
    dropped = len(items) - len(shown)
    if dropped > 0:
        lines.append(f"…(+{dropped} more via 'graph learnings --topic')")
    return "\n".join(lines)


def _by_recency(items: list[dict]) -> list[dict]:
    """Order learnings items most-recent-first by ``completed_at`` (ISO strings
    sort lexically); items with no completion time (unverified) sort last."""
    return sorted(items, key=lambda it: (it.get("completed_at") or ""), reverse=True)


def _aggregate_dep_learnings(db, dep_topic_ids: list[str]) -> list[dict]:
    """Pool every dependency TOPIC's task learnings into one list, ordered
    most-recent-first (build_topic_hydration caps at 10 + breadcrumbs the tail).
    Reuses db_graph.read_learnings (no duplicate SQL)."""
    from dbops import db_graph

    items: list[dict] = []
    for tid in dep_topic_ids:
        items.extend(db_graph.read_learnings(db, topic_id=tid)["items"])
    return _by_recency(items)
