"""Lazy DAG loader for the cockpit graph panel.

Loads ALL projects' topic graphs from graph_topics / graph_tasks /
graph_edges — ONLY when graph mode is active (snapshot(load_graph_dag=True)).
Extracted from juggle_cockpit_model to keep that module under its LOC budget.
Read-only; degrades to None / [] on pre-migration DBs or projects with no tasks.

Topic tier (R5/R9): DAG tasks are TOPICS, edges are derived topic deps, task
counts per topic are attached as tasks_done/tasks_total on GraphTask. The flat
task list per topic is stored in GraphDag.member_tasks for the detail modal.

P7: per-project arming is removed — all projects with tasks are shown.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ARMED_PROJECT_SETTING = "autopilot_armed_project"  # kept for compat reads


@dataclass(frozen=True)
class GraphDag:
    """Lazily-loaded DAG for one project (graph mode only). Read-only."""

    project_id: str
    tasks: list  # list[GraphTask] — TOPICS as tasks (DAG vertices)
    edges: list[tuple[str, str]]  # (topic_id, dep_topic_id)
    member_tasks: "dict | None" = field(default=None, compare=False)  # topic_id → list[dict]
    project_name: "str | None" = None  # human project name for the panel header


def _all_project_ids(conn) -> list[str]:
    """All project ids that have graph work, ordered by last_active DESC then
    alphabetically. Primary source is nodes (P8); legacy tables kept as fallback
    for pre-migration DBs with graph_topics/graph_tasks but no nodes rows."""
    try:
        proj_rows = conn.execute(
            "SELECT id FROM projects WHERE status='active' "
            "ORDER BY last_active DESC, id"
        ).fetchall()
        listed = [r[0] for r in proj_rows]
        extra: set[str] = set()
        # P8 primary: nodes table
        try:
            for r in conn.execute(
                "SELECT DISTINCT project_id FROM nodes "
                "WHERE kind IN ('task','research') AND parent_id IS NULL "
                "AND project_id IS NOT NULL"
            ).fetchall():
                pid = r[0]
                if pid and pid not in listed:
                    extra.add(pid)
        except Exception:
            pass
        # Compat: legacy tables for pre-migration DBs
        for tbl in ("graph_topics", "graph_tasks"):
            try:
                for r in conn.execute(
                    f"SELECT DISTINCT project_id FROM {tbl} "
                    f"WHERE project_id IS NOT NULL"
                ).fetchall():
                    pid = r[0]
                    if pid and pid not in listed:
                        extra.add(pid)
            except Exception:
                pass
        return listed + sorted(extra)
    except Exception:
        return []


def _project_name(conn, pid: str) -> "str | None":
    """Human name for a project id, or None when absent / pre-migration."""
    try:
        row = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (pid,)
        ).fetchone()
    except Exception:
        return None
    return (row[0] if row else None) or None


def _load_one_legacy_tasks(conn, pid: str) -> "GraphDag | None":
    """Fallback: load flat graph_tasks DAG (pre-topic DBs / task-only projects)."""
    from juggle_cockpit_graph_layout import GraphTask

    try:
        task_rows = conn.execute(
            "SELECT n.id, n.title, n.state, n.thread_id, t.user_label "
            "FROM graph_tasks n LEFT JOIN threads t ON n.thread_id = t.id "
            "WHERE n.project_id=? ORDER BY n.created_at, n.id",
            (pid,),
        ).fetchall()
        if not task_rows:
            return None
        ids = tuple(r["id"] for r in task_rows)
        ph = ",".join("?" * len(ids))
        edge_rows = conn.execute(
            f"SELECT task_id, depends_on_id FROM graph_edges WHERE task_id IN ({ph})",
            ids,
        ).fetchall()
    except Exception:
        return None
    tasks = [
        GraphTask(id=r["id"], title=r["title"] or r["id"], state=r["state"],
                  thread_id=r["thread_id"], user_label=r["user_label"])
        for r in task_rows
    ]
    edges = [(r["task_id"], r["depends_on_id"]) for r in edge_rows]
    return GraphDag(project_id=pid, tasks=tasks, edges=edges, member_tasks=None,
                    project_name=_project_name(conn, pid))


def _load_one(conn, pid: str) -> "GraphDag | None":
    """Load topic-tier DAG for one project from nodes (P8); falls back to task-tier.

    nodes is the primary source. For display enrichment (user_label, is_mirror)
    we LEFT JOIN graph_topics + threads — those legacy tables are kept in P8.
    """
    from juggle_cockpit_graph_layout import GraphTask

    try:
        # P8: read root task nodes; enrich with legacy display data where available.
        topic_rows = conn.execute(
            "SELECT n.id, n.title, n.state, "
            "gt.thread_id, t.user_label, "
            "COALESCE(gt.is_mirror, 0) AS is_mirror "
            "FROM nodes n "
            "LEFT JOIN graph_topics gt ON gt.id = n.id "
            "LEFT JOIN threads t ON t.id = gt.thread_id "
            "WHERE n.kind='task' AND n.parent_id IS NULL AND n.project_id=? "
            "ORDER BY n.created_at, n.id",
            (pid,),
        ).fetchall()
    except Exception:
        return _load_one_legacy_tasks(conn, pid)
    if not topic_rows:
        return _load_one_legacy_tasks(conn, pid)

    # Per-parent child counts from nodes.
    topic_ids = tuple(r["id"] for r in topic_rows)
    ph = ",".join("?" * len(topic_ids))
    try:
        count_rows = conn.execute(
            "SELECT parent_id, "
            "SUM(CASE WHEN state='verified' THEN 1 ELSE 0 END) AS done, "
            "COUNT(*) AS total "
            f"FROM nodes WHERE parent_id IN ({ph}) GROUP BY parent_id",
            topic_ids,
        ).fetchall()
    except Exception:
        count_rows = []
    counts: dict[str, tuple[int, int]] = {
        r["parent_id"]: (r["done"], r["total"]) for r in count_rows
    }

    # Derived edges: node_edges that cross parent boundaries.
    try:
        edge_rows = conn.execute(
            "SELECT DISTINCT np.id AS src, dp.id AS dst "
            "FROM node_edges e "
            "JOIN nodes nc ON nc.id = e.node_id "
            "JOIN nodes np ON np.id = nc.parent_id "
            "JOIN nodes dc ON dc.id = e.depends_on_id "
            "JOIN nodes dp ON dp.id = dc.parent_id "
            f"WHERE np.id IN ({ph}) AND dp.id IN ({ph}) AND np.id != dp.id "
            "ORDER BY np.id, dp.id",
            (*topic_ids, *topic_ids),
        ).fetchall()
    except Exception:
        edge_rows = []
    edges = [(r["src"], r["dst"]) for r in edge_rows]

    # Children per parent (member_tasks for the detail modal).
    try:
        child_rows = conn.execute(
            "SELECT id, title, state, parent_id "
            f"FROM nodes WHERE parent_id IN ({ph}) ORDER BY created_at, id",
            topic_ids,
        ).fetchall()
    except Exception:
        child_rows = []
    member_tasks: dict[str, list] = {tid: [] for tid in topic_ids}
    for r in child_rows:
        if r["parent_id"] in member_tasks:
            member_tasks[r["parent_id"]].append(
                {"id": r["id"], "title": r["title"], "state": r["state"]}
            )

    tasks = []
    for r in topic_rows:
        tid = r["id"]
        done, total = counts.get(tid, (0, 0))
        tasks.append(GraphTask(
            id=tid,
            title=r["title"] or tid,
            state=r["state"],
            thread_id=r["thread_id"],
            user_label=r["user_label"],
            tasks_done=done or None,
            tasks_total=total or None,
            is_mirror=bool(r["is_mirror"]),
        ))

    return GraphDag(project_id=pid, tasks=tasks, edges=edges, member_tasks=member_tasks,
                    project_name=_project_name(conn, pid))


def load_graph_dags(conn) -> list["GraphDag"]:
    """Load topic-tier DAGs for ALL active projects with tasks, in priority order."""
    result = []
    for pid in _all_project_ids(conn):
        dag = _load_one(conn, pid)
        if dag is not None:
            result.append(dag)
    return result


def load_graph_dag(conn) -> "GraphDag | None":
    """COMPAT SHIM: first project's DAG, or None (legacy single-project callers)."""
    dags = load_graph_dags(conn)
    return dags[0] if dags else None
