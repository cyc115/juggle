"""Lazy DAG loader for the cockpit graph panel.

Loads ALL projects' topic graphs from the unified ``nodes`` / ``node_edges``
tables (P8 Task 4.2) — ONLY when graph mode is active
(snapshot(load_graph_dag=True)). Extracted from juggle_cockpit_model to keep that
module under its LOC budget. Read-only; degrades to None / [] on projects with no
task nodes.

Topic tier (R5/R9): DAG tasks are TOPICS, edges derived topic deps, per-topic
task counts on GraphTask, member_tasks per topic for the detail modal. P7:
per-project arming removed — all projects with tasks are shown.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from juggle_cockpit_graph_activity import gather_project_activity
from juggle_cockpit_graph_order import OrderCache, order_projects

ARMED_PROJECT_SETTING = "autopilot_armed_project"  # kept for compat reads

# Live cockpit snapshot cache for the graph-panel order (throttle). Tests inject
# their own OrderCache into order_projects, so this global is never shared.
_ORDER_CACHE = OrderCache()


@dataclass(frozen=True)
class GraphDag:
    """Lazily-loaded DAG for one project (graph mode only). Read-only."""

    project_id: str
    tasks: list  # list[GraphTask] — TOPICS as tasks (DAG vertices)
    edges: list[tuple[str, str]]  # (topic_id, dep_topic_id)
    member_tasks: "dict | None" = field(default=None, compare=False)  # topic_id → list[dict]
    project_name: "str | None" = None  # human project name for the panel header


def _project_name(conn, pid: str) -> "str | None":
    """Human name for a project id, or None when absent / pre-migration."""
    try:
        row = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (pid,)
        ).fetchone()
    except Exception:
        return None
    return (row[0] if row else None) or None


def _load_one(conn, pid: str) -> "GraphDag | None":
    """Load topic-tier DAG for one project purely from the unified nodes table.

    A DAG root is a kind='topic' node OR a parentless kind='task' node — a bare
    task added to a project without an owning topic still renders (P8 M2). The bound
    dispatch thread is the typed kind='dispatch' node_edge (P8 M1/Q2); its
    user_label comes from the bound conversation node (JOIN through the edge). No
    legacy graph_* / threads tables are read (P8 Task 4.2). Returns None for a
    project with no task nodes.
    """
    from juggle_cockpit_graph_layout import GraphTask

    try:
        topic_rows = conn.execute(
            "SELECT n.id, n.title, n.state, "
            "de.depends_on_id AS thread_id, c.user_label "
            "FROM nodes n "
            "LEFT JOIN node_edges de "
            "  ON de.node_id = n.id AND de.kind='dispatch' "
            "LEFT JOIN nodes c "
            "  ON c.id = de.depends_on_id AND c.kind='conversation' "
            "WHERE (n.kind='topic' OR (n.kind='task' AND n.parent_id IS NULL)) "
            "AND n.project_id=? "
            "ORDER BY n.created_at, n.id",
            (pid,),
        ).fetchall()
    except Exception:
        return None
    if not topic_rows:
        return None

    # Per-parent child counts from nodes.
    topic_ids = tuple(r["id"] for r in topic_rows)
    ph = ",".join("?" * len(topic_ids))
    try:
        count_rows = conn.execute(
            "SELECT parent_id, "
            "SUM(CASE WHEN state IN ('verified','delivered','cancelled') THEN 1 ELSE 0 END) AS done, "
            "COUNT(*) AS total "
            f"FROM nodes WHERE parent_id IN ({ph}) GROUP BY parent_id",
            topic_ids,
        ).fetchall()
    except Exception:
        count_rows = []
    counts: dict[str, tuple[int, int]] = {
        r["parent_id"]: (r["done"], r["total"]) for r in count_rows
    }

    # Derived edges: node_edges that cross parent boundaries (topic→topic deps).
    try:
        edge_rows = conn.execute(
            "SELECT DISTINCT np.id AS src, dp.id AS dst "
            "FROM node_edges e "
            "JOIN nodes nc ON nc.id = e.node_id "
            "JOIN nodes np ON np.id = nc.parent_id "
            "JOIN nodes dc ON dc.id = e.depends_on_id "
            "JOIN nodes dp ON dp.id = dc.parent_id "
            f"WHERE e.kind='dep' AND np.id IN ({ph}) AND dp.id IN ({ph}) AND np.id != dp.id "
            "ORDER BY np.id, dp.id",
            (*topic_ids, *topic_ids),
        ).fetchall()
    except Exception:
        edge_rows = []
    edges = [(r["src"], r["dst"]) for r in edge_rows]
    # Direct edges between two root nodes — a flat task-tier DAG whose tasks have
    # no parent topic (the legacy flat-task projects). The cross-parent join
    # above misses these because both endpoints have parent_id NULL.
    try:
        root_edge_rows = conn.execute(
            f"SELECT node_id AS src, depends_on_id AS dst FROM node_edges "
            f"WHERE kind='dep' AND node_id IN ({ph}) AND depends_on_id IN ({ph}) "
            "ORDER BY node_id, depends_on_id",
            (*topic_ids, *topic_ids),
        ).fetchall()
    except Exception:
        root_edge_rows = []
    for r in root_edge_rows:
        e = (r["src"], r["dst"])
        if e not in edges:
            edges.append(e)

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
        ))

    return GraphDag(project_id=pid, tasks=tasks, edges=edges, member_tasks=member_tasks,
                    project_name=_project_name(conn, pid))


def _sort_interval() -> float:
    """Snapshot throttle interval (secs) from cockpit.graph_sort_interval_seconds.
    Fail-soft to the 60s default; <=0 means recompute every render."""
    try:
        from juggle_settings import get_settings

        return float(get_settings()["cockpit"]["graph_sort_interval_seconds"])
    except Exception:
        return 60.0


def load_graph_dags(conn, *, now: "float | None" = None) -> list["GraphDag"]:
    """Load topic-tier DAGs for ALL active projects with tasks, in sort order.

    ``now`` is the injected monotonic clock. When supplied (live cockpit) the
    order is throttled through the module snapshot cache at the configured
    interval so the panel does not reshuffle on every render. When ``None``
    (tests / ad-hoc callers) the order is recomputed fresh with no throttle.
    """
    rows = gather_project_activity(conn)
    if now is None:
        ordered = order_projects(rows, now=0.0, interval=0.0, cache=OrderCache())
    else:
        ordered = order_projects(
            rows, now=now, interval=_sort_interval(), cache=_ORDER_CACHE
        )
    result = []
    for pid in ordered:
        dag = _load_one(conn, pid)
        if dag is not None:
            result.append(dag)
    return result


def load_graph_dag(conn) -> "GraphDag | None":
    """COMPAT SHIM: first project's DAG, or None (legacy single-project callers)."""
    dags = load_graph_dags(conn)
    return dags[0] if dags else None
