"""Lazy DAG loader for the cockpit graph panel.

Loads the armed project's task graph (nodes + edges) from graph_nodes /
graph_edges — ONLY when graph mode is active (snapshot(load_graph_dag=True)).
Extracted from juggle_cockpit_model to keep that module under its LOC budget.
Read-only; degrades to None on pre-migration DBs or when no project is armed.
"""
from __future__ import annotations

from dataclasses import dataclass

ARMED_PROJECT_SETTING = "autopilot_armed_project"


@dataclass(frozen=True)
class GraphDag:
    """Lazily-loaded DAG for the armed project (graph mode only). Read-only."""

    project_id: str
    nodes: list  # list[juggle_cockpit_graph_layout.GraphNode]
    edges: list[tuple[str, str]]  # (node_id, depends_on_id)


def load_graph_dag(conn) -> "GraphDag | None":
    """Load the armed project's DAG (nodes + edges) — only called in graph mode.

    Armed project = settings key autopilot_armed_project. Returns None when no
    project is armed or it has no graph nodes. Pre-migration DBs degrade to None.
    """
    from juggle_cockpit_graph_layout import GraphNode

    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (ARMED_PROJECT_SETTING,)
        ).fetchone()
    except Exception:
        return None
    armed = ((row[0] if row else "") or "").strip()
    if not armed:
        return None
    try:
        node_rows = conn.execute(
            "SELECT id, title, state, thread_id FROM graph_nodes WHERE project_id=? "
            "ORDER BY created_at, id",
            (armed,),
        ).fetchall()
        if not node_rows:
            return None
        ids = tuple(r["id"] for r in node_rows)
        ph = ",".join("?" * len(ids))
        edge_rows = conn.execute(
            f"SELECT node_id, depends_on_id FROM graph_edges WHERE node_id IN ({ph})",
            ids,
        ).fetchall()
    except Exception:
        return None
    nodes = [
        GraphNode(
            id=r["id"], title=r["title"] or r["id"], state=r["state"],
            thread_id=r["thread_id"],
        )
        for r in node_rows
    ]
    edges = [(r["node_id"], r["depends_on_id"]) for r in edge_rows]
    return GraphDag(project_id=armed, nodes=nodes, edges=edges)
