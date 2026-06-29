"""dbops.migration_parent_relink — heal residual NULL ``nodes.parent_id`` from the
legacy graph_tasks snapshot (DEFECT #4907, 2026-06-21).

Incident: a graph (re)load wrote task nodes but left ``nodes.parent_id`` NULL
while ``graph_tasks.topic_id`` was correct — the parent_id dual-write
(db_graph.set_task_topic) only landed in the C2 read-flip, so pre-flip nodes
drifted, and a re-load skips set_task_topic for protected/unchanged tasks (it
never re-links them). ``find_unmerged_completed_topics`` reads
``nodes WHERE parent_id=topic_id``, so a verified topic looked CHILDLESS →
``reconcile_out_of_band_merges`` never stamped merged_sha → the watchdog
re-dispatched the already-completed step in a loop.

This heals ONLY a still-NULL ``nodes.parent_id`` from the frozen-but-correct
``graph_tasks.topic_id`` (the residual pre-read-flip drift).

P8 c4-write-cut (2026-06-29): ``nodes`` is now the SOLE authoritative store —
``db_graph.create_task``/``set_task_topic``/``task_transition`` write nodes only,
so ``graph_tasks`` is FROZEN. Two consequences for this repair:
  • The state resync (``nodes.state`` ← ``graph_tasks.state``) is REMOVED: with a
    frozen graph_tasks it would REVERT a live, legitimately-advanced node state
    back to the stale legacy value — a worse bug than the one it healed.
  • The parent relink is restricted to rows where ``nodes.parent_id`` is still
    NULL/empty, so a correctly-set parent_id is never reverted to a stale value.
New drift can no longer occur (parent_id is set directly on nodes at creation),
so this only heals rows stranded before the read-flip. Idempotent and drop-safe
(no-op once graph_tasks is gone) — it lives in the migration namespace
(Gate A excludes ``dbops/migration*.py``), not as a steady-state graph_tasks
reader.
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
    """Heal a still-NULL ``nodes.parent_id`` ← ``graph_tasks.topic_id`` for task
    nodes that have a legacy row. Optional ``project_id`` scopes the repair.

    Idempotent. Returns ``{'parent_relinked': int, 'state_resynced': int}`` — the
    number of task nodes re-linked (``state_resynced`` is always 0; the state
    resync was removed at the P8 c4-write-cut — see the module docstring). No-op
    when graph_tasks is absent (post-drop).

    Only NULL/empty parent_id is healed: a correctly-set parent_id is NEVER
    overwritten from the now-frozen graph_tasks (which would revert a live value).
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
            # Heal ONLY a still-NULL parent_id (post-write-cut nodes is authoritative;
            # never revert a set parent_id from the frozen legacy snapshot).
            "  AND IFNULL(parent_id,'') = '' "
            "  AND EXISTS (SELECT 1 FROM graph_tasks g WHERE g.id=nodes.id) "
            "  AND IFNULL((SELECT g.topic_id FROM graph_tasks g WHERE g.id=nodes.id),'') != ''"
            + scope,
            (now, *pargs),
        ).rowcount
    return {"parent_relinked": relinked, "state_resynced": 0}


def parent_reconcile_summary(db) -> str:
    """Run the reconcile and return a one-line summary (doctor pass)."""
    pc = reconcile_node_parentage(db)
    if pc["parent_relinked"] or pc["state_resynced"]:
        return (
            f"graph parentage: {pc['parent_relinked']} parent link(s) re-linked, "
            f"{pc['state_resynced']} state(s) resynced from graph_tasks"
        )
    return "graph parentage: all task nodes consistent"
