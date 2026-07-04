"""dbops.orphan_reconcile — stamp merged_sha for completed topics already on main.

Extracted from ``dbops.orphan_guard`` (2026-07-03, LOC gate + separation of
concerns): orphan_guard's docstring promises "pure detection + flagging … owns
no state transition", yet ``reconcile_out_of_band_merges`` WRITES state
(``UPDATE nodes SET state='verified'``). It is the RECONCILER, so it lives here.
Re-exported from orphan_guard so the existing call surface
(``orphan_guard.reconcile_out_of_band_merges``, used by juggle_cmd_doctor + the
flag path + tests) is unchanged.

Must not own: orphan DETECTION (find_unmerged_completed_topics stays in
orphan_guard) or the flagging path — this module only advances topics whose work
is provably on main.
"""

from __future__ import annotations

from datetime import datetime, timezone

from dbops.graph_guards import resolve_landed_sha


def reconcile_out_of_band_merges(db, *, main: str = "main") -> list[str]:
    """Stamp ``merged_sha`` + advance to 'verified' for completed topics whose
    work is already on main. Returns reconciled node ids.

    Two-tier LANDED verification (2026-07-03 amendment): advances a topic whose
    branch is an ancestor of main (ff/true-merge) OR whose commits have a
    content-equivalent on main (rebased/cherry-picked landing) — recording a real
    ancestor sha in both cases. Genuinely-unmerged work (branch ahead of main /
    ref gone) resolves to '' and is left untouched.

    Self-heals graph drift FIRST (DEFECT #4907): a task node with a NULL
    parent_id makes its topic look childless to ``find_unmerged_completed_topics``
    so it is never reconciled. Relink a still-NULL parent_id from the frozen
    graph_tasks.topic_id before detecting, so a stranded-but-completed topic is
    found and stamped. (P8 c4-write-cut: nodes.state is authoritative and is NOT
    resynced from the frozen legacy table.)
    """
    from dbops import db_topics
    from dbops.migration_parent_relink import reconcile_node_parentage
    from dbops.orphan_guard import (
        _node_repo, _topic_branch, find_unmerged_completed_topics,
    )

    reconcile_node_parentage(db)

    reconciled: list[str] = []
    for node in find_unmerged_completed_topics(db):
        repo = _node_repo(db, node)
        branch = _topic_branch(db, node)
        if not repo or not branch:
            continue
        sha = resolve_landed_sha(repo, branch, main=main)
        if not sha:
            continue
        now = datetime.now(timezone.utc).isoformat()
        with db._connect() as conn:
            # nodes is authoritative; set_topic_merged_sha already lockstep-mirrors
            # graph_topics, so stamp the node directly here (single store).
            conn.execute(
                "UPDATE nodes SET merged_sha=?, state='verified', updated_at=? WHERE id=?",
                (sha, now, node["id"]),
            )
            conn.commit()
        # Re-derive the topic state from its member tasks (idempotent on verified).
        try:
            db_topics.reconcile_topic_state(db, node["id"])
        except Exception:
            pass
        reconciled.append(node["id"])
    return reconciled
