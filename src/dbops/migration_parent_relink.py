"""dbops.migration_parent_relink — repair drift between the legacy graph_tasks
table and the authoritative task nodes (DEFECT #4907, 2026-06-21).

Incident: a graph (re)load wrote task nodes but left ``nodes.parent_id`` NULL
while ``graph_tasks.topic_id`` was correct — the parent_id dual-write
(db_graph.set_task_topic) only landed in the C2 read-flip, so pre-flip nodes
drifted, and a re-load skips set_task_topic for protected/unchanged tasks (it
never re-links them). ``find_unmerged_completed_topics`` reads
``nodes WHERE parent_id=topic_id``, so a verified topic looked CHILDLESS →
``reconcile_out_of_band_merges`` never stamped merged_sha → the watchdog
re-dispatched the already-completed step in a loop. Task-node state could drift
the same way (nodes 'ready' vs legacy 'verified').

This re-links ``nodes.parent_id`` from ``graph_tasks.topic_id`` and resyncs
task-node state from the legacy authoritative ``graph_tasks.state``. graph_tasks
is the trustworthy snapshot for repair: going forward ``db_graph.task_transition``
lockstep-writes both stores, so no new drift is introduced; this only heals rows
stranded before lockstep existed. Idempotent (re-running on a consistent DB
yields zeros) and drop-safe (no-op once graph_tasks is gone) — hence it lives in
the migration namespace alongside the other legacy-reading backfills, NOT as a
steady-state graph_tasks reader (Gate A excludes ``dbops/migration*.py``).
"""

from __future__ import annotations

from contextlib import contextmanager

from dbops.schema import _now


@contextmanager
def _cx(db, conn=None):
    """Yield a write connection. A caller-passed ``conn`` owns the transaction
    (no commit — composes into the loader's all-or-nothing load); else open,
    commit, close."""
    if conn is not None:
        yield conn
        return
    c = db._connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _has_graph_tasks(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_tasks'"
    ).fetchone() is not None


def reconcile_node_parentage(db, *, project_id=None, conn=None) -> dict:
    """Re-link ``nodes.parent_id`` ← ``graph_tasks.topic_id`` and resync
    ``nodes.state`` ← ``graph_tasks.state`` for every task node that disagrees
    with its legacy authoritative row. Optional ``project_id`` scopes the repair.

    Idempotent. Returns ``{'parent_relinked': int, 'state_resynced': int}`` — the
    number of task nodes actually changed (the WHERE filters to divergent rows, so
    a consistent DB yields zeros). No-op when graph_tasks is absent (post-drop).

    State is synced verbatim: the legacy task-entry vocab is already unified to
    the node vocab in BOTH stores by Migration 51 (the dead pending state is
    rewritten to open), which always runs before any caller here (doctor/loader
    init_db, watchdog on a migrated DB), so no value translation is needed.
    """
    now = _now()
    scope = "" if project_id is None else " AND project_id=?"
    pargs = () if project_id is None else (project_id,)
    with _cx(db, conn) as c:
        if not _has_graph_tasks(c):
            return {"parent_relinked": 0, "state_resynced": 0}
        relinked = c.execute(
            "UPDATE nodes SET "
            "  parent_id=(SELECT g.topic_id FROM graph_tasks g WHERE g.id=nodes.id), "
            "  updated_at=? "
            "WHERE kind='task' "
            "  AND EXISTS (SELECT 1 FROM graph_tasks g WHERE g.id=nodes.id) "
            "  AND IFNULL(parent_id,'') != "
            "      IFNULL((SELECT g.topic_id FROM graph_tasks g WHERE g.id=nodes.id),'')"
            + scope,
            (now, *pargs),
        ).rowcount
        resynced = c.execute(
            "UPDATE nodes SET "
            "  state=(SELECT g.state FROM graph_tasks g WHERE g.id=nodes.id), "
            "  updated_at=? "
            "WHERE kind='task' "
            "  AND EXISTS (SELECT 1 FROM graph_tasks g WHERE g.id=nodes.id) "
            "  AND state != (SELECT g.state FROM graph_tasks g WHERE g.id=nodes.id)"
            + scope,
            (now, *pargs),
        ).rowcount
    return {"parent_relinked": relinked, "state_resynced": resynced}


def parent_reconcile_summary(db) -> str:
    """Run the reconcile and return a one-line summary (doctor pass)."""
    pc = reconcile_node_parentage(db)
    if pc["parent_relinked"] or pc["state_resynced"]:
        return (
            f"graph parentage: {pc['parent_relinked']} parent link(s) re-linked, "
            f"{pc['state_resynced']} state(s) resynced from graph_tasks"
        )
    return "graph parentage: all task nodes consistent"
