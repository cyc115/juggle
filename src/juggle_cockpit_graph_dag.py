"""Lazy DAG loader for the cockpit graph panel.

Loads the armed project(s) topic graph from graph_topics / graph_nodes /
graph_edges — ONLY when graph mode is active (snapshot(load_graph_dag=True)).
Extracted from juggle_cockpit_model to keep that module under its LOC budget.
Read-only; degrades to None / [] on pre-migration DBs or when no project is armed.

Topic tier (R5/R9): DAG nodes are TOPICS, edges are derived topic deps, task
counts per topic are attached as tasks_done/tasks_total on GraphNode. The flat
task list per topic is stored in GraphDag.tasks for the detail modal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ARMED_PROJECT_SETTING = "autopilot_armed_project"


@dataclass(frozen=True)
class GraphDag:
    """Lazily-loaded DAG for one armed project (graph mode only). Read-only."""

    project_id: str
    nodes: list  # list[GraphNode] — TOPICS as nodes
    edges: list[tuple[str, str]]  # (topic_id, dep_topic_id)
    tasks: "dict | None" = field(default=None, compare=False)  # topic_id → list[dict]


def _armed_set(conn) -> list[str]:
    """CSV parse of the settings value — mirrors juggle_autopilot_state accessor."""
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (ARMED_PROJECT_SETTING,)
        ).fetchone()
    except Exception:
        return []
    raw = ((row[0] if row else "") or "").strip()
    out: list[str] = []
    for part in raw.split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def _load_one_legacy_nodes(conn, pid: str) -> "GraphDag | None":
    """Fallback: load flat graph_nodes DAG (pre-topic DBs / node-only projects)."""
    from juggle_cockpit_graph_layout import GraphNode

    try:
        node_rows = conn.execute(
            "SELECT n.id, n.title, n.state, n.thread_id, t.user_label "
            "FROM graph_nodes n LEFT JOIN threads t ON n.thread_id = t.id "
            "WHERE n.project_id=? ORDER BY n.created_at, n.id",
            (pid,),
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
        GraphNode(id=r["id"], title=r["title"] or r["id"], state=r["state"],
                  thread_id=r["thread_id"], user_label=r["user_label"])
        for r in node_rows
    ]
    edges = [(r["node_id"], r["depends_on_id"]) for r in edge_rows]
    return GraphDag(project_id=pid, nodes=nodes, edges=edges, tasks=None)


def _load_one(conn, pid: str) -> "GraphDag | None":
    """Load topic-tier DAG for one armed project; falls back to node-tier."""
    from juggle_cockpit_graph_layout import GraphNode

    try:
        topic_rows = conn.execute(
            "SELECT id, title, state, thread_id FROM graph_topics "
            "WHERE project_id=? ORDER BY created_at, id",
            (pid,),
        ).fetchall()
    except Exception:
        return _load_one_legacy_nodes(conn, pid)
    if not topic_rows:
        return _load_one_legacy_nodes(conn, pid)

    # Per-topic task counts.
    try:
        count_rows = conn.execute(
            "SELECT topic_id, "
            "SUM(CASE WHEN state='verified' THEN 1 ELSE 0 END) AS done, "
            "COUNT(*) AS total "
            "FROM graph_nodes WHERE topic_id IS NOT NULL "
            "AND project_id=? GROUP BY topic_id",
            (pid,),
        ).fetchall()
    except Exception:
        count_rows = []
    counts: dict[str, tuple[int, int]] = {
        r["topic_id"]: (r["done"], r["total"]) for r in count_rows
    }

    # Derived topic edges: task edges that cross topic boundaries.
    topic_ids = tuple(r["id"] for r in topic_rows)
    ph = ",".join("?" * len(topic_ids))
    try:
        edge_rows = conn.execute(
            "SELECT DISTINCT n.topic_id AS src, d.topic_id AS dst "
            "FROM graph_edges e "
            "JOIN graph_nodes n ON n.id = e.node_id "
            "JOIN graph_nodes d ON d.id = e.depends_on_id "
            f"WHERE n.topic_id IN ({ph}) AND d.topic_id IN ({ph}) "
            "AND n.topic_id != d.topic_id "
            "ORDER BY n.topic_id, d.topic_id",
            (*topic_ids, *topic_ids),
        ).fetchall()
    except Exception:
        edge_rows = []
    edges = [(r["src"], r["dst"]) for r in edge_rows]

    # Task lists per topic (for the detail modal).
    try:
        task_rows = conn.execute(
            "SELECT id, title, state, topic_id FROM graph_nodes "
            f"WHERE topic_id IN ({ph}) ORDER BY created_at, id",
            topic_ids,
        ).fetchall()
    except Exception:
        task_rows = []
    tasks: dict[str, list] = {tid: [] for tid in topic_ids}
    for r in task_rows:
        if r["topic_id"] in tasks:
            tasks[r["topic_id"]].append(
                {"id": r["id"], "title": r["title"], "state": r["state"]}
            )

    nodes = []
    for r in topic_rows:
        tid = r["id"]
        done, total = counts.get(tid, (0, 0))
        nodes.append(GraphNode(
            id=tid,
            title=r["title"] or tid,
            state=r["state"],
            thread_id=r["thread_id"],
            tasks_done=done or None,
            tasks_total=total or None,
        ))

    return GraphDag(project_id=pid, nodes=nodes, edges=edges, tasks=tasks)


def load_graph_dags(conn) -> list["GraphDag"]:
    """Load topic-tier DAGs for all armed projects (CSV settings key), in order."""
    result = []
    for pid in _armed_set(conn):
        dag = _load_one(conn, pid)
        if dag is not None:
            result.append(dag)
    return result


def load_graph_dag(conn) -> "GraphDag | None":
    """COMPAT SHIM: first armed project's DAG, or None (legacy single-armed callers)."""
    dags = load_graph_dags(conn)
    return dags[0] if dags else None
