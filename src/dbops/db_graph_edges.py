"""dbops.db_graph_edges — task dependency-edge CRUD over node_edges (P8).

Extracted from dbops.db_graph (architecture LOC gate). The task DAG edges live in
``node_edges`` (authoritative); the legacy ``graph_edges`` mirror write was cut by
the P8 terminal drop (Migration 55), which retires the table entirely.
Re-exported from dbops.db_graph so ``from dbops.db_graph import get_deps`` works.
"""
from __future__ import annotations


def replace_edges(db, task_id: str, dep_ids: list[str], conn=None) -> None:
    """Replace the full dependency list of ``task_id`` in ``node_edges``."""
    from dbops.db_graph import _cx

    with _cx(db, conn) as c:
        # Only dependency edges — the task's kind='dispatch' binding is preserved.
        c.execute("DELETE FROM node_edges WHERE node_id=? AND kind='dep'", (task_id,))
        c.executemany(
            "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id, kind) "
            "VALUES (?,?,'dep')",
            [(task_id, dep) for dep in dep_ids],
        )


def get_deps(db, task_id: str) -> list[str]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT depends_on_id FROM node_edges WHERE node_id=? AND kind='dep' "
            "ORDER BY depends_on_id",
            (task_id,),
        ).fetchall()
        return [r["depends_on_id"] for r in rows]


def get_dependents(db, task_id: str) -> list[str]:
    """Task ids that depend on ``task_id`` (reverse edges)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT node_id FROM node_edges WHERE depends_on_id=? AND kind='dep' "
            "ORDER BY node_id",
            (task_id,),
        ).fetchall()
        return [r["node_id"] for r in rows]


def unverified_deps(db, task_id: str) -> list[str]:
    """Dep ids of ``task_id`` whose state is not 'verified' (blocking deps)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT e.depends_on_id FROM node_edges e "
            "JOIN nodes d ON d.id = e.depends_on_id "
            "WHERE e.node_id=? AND e.kind='dep' AND d.state != 'verified' "
            "ORDER BY e.depends_on_id",
            (task_id,),
        ).fetchall()
        return [r["depends_on_id"] for r in rows]
