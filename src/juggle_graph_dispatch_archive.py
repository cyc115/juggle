"""juggle_graph_dispatch_archive — archive a graph-dispatch failure without
stranding a real worktree or a phantom worktree_branch stamp.

Extracted from juggle_graph_dispatch (architecture-gate LOC budget).
Owns: _archive_dispatch_failure. Must not own: claim/dispatch/hydration.
"""
from __future__ import annotations


def _archive_dispatch_failure(db, thread_id: str) -> None:
    """Archive a thread whose ``dispatch()`` call raised, without stranding a
    worktree reference the completion pipeline never gets a chance to see.

    Bug#1 attempt#4 (2026-07-20): a dispatch failure archives the thread
    directly (never through ``cmd_complete_agent``/``finalize_or_detach_
    integrate``/``_finalize_worktree``), so a real worktree created moments
    earlier at :func:`juggle_dispatch_worktree_context.build_worktree_context`
    was silently abandoned — checkout, branch, and any commits left
    unmerged with zero finalize attempt. Separately, the dispatch-time
    ``worktree_branch`` pre-stamp (hole #3, in ``juggle_graph_dispatch``) is
    written BEFORE the worktree is confirmed to exist; when ``dispatch()``
    fails before that worktree is created, the stamp survives archival
    alone — worktree_branch set, worktree_path/main_repo_path empty — the
    exact live-DB shape from the confirmed-broken finding. Finalizing first
    (best-effort; never raises) and clearing an unbacked stamp keeps every
    archived node honest: either its branch was actually merged, or it
    never carried one.
    """
    from juggle_cmd_agents_worktree import _finalize_worktree

    thread = db.get_thread(thread_id) or {}
    if thread.get("worktree_path") and thread.get("main_repo_path"):
        _finalize_worktree(thread)  # best-effort ff-merge + cleanup
    elif thread.get("worktree_branch") and not thread.get("main_repo_path"):
        db.update_thread(thread_id, worktree_branch="")
    db.archive_thread(thread_id)
