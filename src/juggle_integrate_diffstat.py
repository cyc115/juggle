"""Juggle — integrate diffstat capture (extracted from juggle_cmd_integrate for
the LOC gate).

Pre-merge diffstat hydration: the only cheap moment to capture it, since
integrate deletes the branch+worktree on success. Best-effort — never a gate.
"""

from __future__ import annotations


def capture_diffstat(db, backend, task: dict | None, worktree_path: str, rebase_onto: str) -> None:
    if not task:
        return
    try:
        from dbops import db_graph
        diffstat = backend.describe_changes(worktree_path, since=rebase_onto)
        if diffstat:
            db_graph.set_task_diffstat(db, task["id"], diffstat)
    except Exception:
        pass  # diffstat is best-effort hydration enrichment, never a gate
