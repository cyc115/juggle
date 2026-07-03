"""juggle_cockpit_tree — shared pure tree-layout helper for the cockpit Topics
pane (2026-06-30 topic-graph-state-unify R3). Renders a parent line plus either
indented child lines (expanded) or a single done/total rollup (collapsed). Pure
Rich; no DB, no Textual."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.text import Text

_MERGED = frozenset({"verified", "done"})
_GRAPH_BOUND_STATES = frozenset({"running", "dispatching"})
_MAX_CHILD_LINES = 6


@dataclass(frozen=True)
class TreeChild:
    id: str
    state: str
    title: str = ""


def _tasks_under(conn, parent_id) -> tuple:
    rows = conn.execute(
        "SELECT id, state, title FROM nodes WHERE kind='task' AND parent_id=? "
        "ORDER BY created_at, id",
        (parent_id,),
    ).fetchall()
    return tuple(TreeChild(r["id"], r["state"], r["title"] or "") for r in rows)


def load_topic_children(conn, parent_id) -> tuple:
    """Load a conversation's child task nodes as TreeChild rows (F7, 2026-06-30
    topic-graph-state-unify).

    Tries tasks parented directly on ``parent_id`` first (the original
    conversation-parented flow). If none are found, resolves the graph topic
    bound to this thread via the typed kind='dispatch' node_edge and, ONLY IF
    that topic is running/dispatching, loads its member tasks instead (2026-07-03
    — Topics pane showed no nesting for graph-dispatched threads, CX)."""
    direct = _tasks_under(conn, parent_id)
    if direct:
        return direct

    topic = conn.execute(
        "SELECT n.id, n.state FROM nodes n "
        "JOIN node_edges e ON e.node_id = n.id "
        "WHERE n.kind='topic' AND e.kind='dispatch' AND e.depends_on_id=?",
        (parent_id,),
    ).fetchone()
    if topic is None or topic["state"] not in _GRAPH_BOUND_STATES:
        return ()

    return _tasks_under(conn, topic["id"])


def tree_lines(
    parent_label: str,
    children: list[TreeChild],
    *,
    expanded: bool,
    glyph_for: Callable[[str], str],
    width: int,  # noqa: ARG001 — reserved for future truncation idiom
) -> list[Text]:
    out: list[Text] = [Text(parent_label, no_wrap=True, overflow="ellipsis")]
    if not children:
        return out
    if expanded:
        overflow = len(children) - _MAX_CHILD_LINES
        shown = children if overflow <= 0 else children[: _MAX_CHILD_LINES - 1]
        for c in shown:
            label = c.title or c.id
            out.append(
                Text(f"  └─ {glyph_for(c.state)} {label}",
                     no_wrap=True, overflow="ellipsis")
            )
        if overflow > 0:
            out.append(
                Text(f"  └─ … +{len(children) - len(shown)} more",
                     no_wrap=True, overflow="ellipsis")
            )
    else:
        done = sum(1 for c in children if c.state in _MERGED)
        out.append(
            Text(f"  └─ {done}/{len(children)} done",
                 no_wrap=True, overflow="ellipsis")
        )
    return out
