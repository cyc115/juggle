"""
juggle_cmd_agents_worktree — Git worktree helpers for agent dispatch/completion.

Owns: _create_worktree (isolated worktree per thread, used by send-task) and
      _finalize_worktree (ff-merge → remove → branch-delete, used by complete-agent).
Must not own: command handler logic or DB access, or juggle naming conventions
      beyond what THIS module computes (``cyc_<label>``, ``juggle-<basename>-
      <label>``) — vcs.py's ``create_workspace``/``remove_workspace`` take the
      already-computed branch/path and do only the mechanical VCS operation.
"""

import subprocess
from pathlib import Path

from vcs import backend_for


def _register_worktree_trust(worktree_path: str) -> None:
    """Pre-register worktree_path as a trusted Claude Code project.

    Back-compat shim. The real logic — and the fix for the 2026-06-20 leak
    (writing the ``hasTrustDialogAccepted`` flag Claude Code actually reads, not
    just ``allowedTools``, which left the trust gate firing and the agent hung)
    — lives in ``juggle_claude_trust.ensure_dir_trusted``. Env var
    JUGGLE_CLAUDE_JSON_PATH still overrides the path (used in tests).
    """
    from juggle_claude_trust import ensure_dir_trusted

    ensure_dir_trusted(worktree_path)


def _finalize_worktree(thread: dict) -> tuple:
    """Finalize a worktree: ff-merge → remove → branch-delete.

    Returns (success: bool, message: str). Never destroys unmerged commits.

    Robust to a lost/never-persisted ``worktree_path`` (2026-07-19 Bug#1
    attempt#3 live-DB finding: many archived threads carry a real
    ``worktree_branch`` + ``main_repo_path`` with ``worktree_path`` empty —
    the ff-merge itself only needs the branch name + main repo, so a missing
    checkout directory must not silently skip the merge and lose the
    branch's commits (the old `not worktree_path` short-circuit did exactly
    that). The checkout directory is still used for full teardown when
    present; otherwise cleanup falls back to a plain branch delete.
    """
    worktree_path = (thread.get("worktree_path") or "").strip()
    worktree_branch = (thread.get("worktree_branch") or "").strip()
    main_repo_path = (thread.get("main_repo_path") or "").strip()

    if not worktree_branch or not main_repo_path:
        return True, ""  # No worktree to finalize

    if worktree_path and not Path(worktree_path).exists():
        return True, f"Worktree already removed: {worktree_path}"

    if not Path(main_repo_path).exists():
        return False, f"Main repo not found: {main_repo_path}"

    if not worktree_path:
        branch_exists = subprocess.run(
            ["git", "-C", main_repo_path, "rev-parse", "--verify", "--quiet",
             f"refs/heads/{worktree_branch}"],
            capture_output=True, text=True,
        ).returncode == 0
        if not branch_exists:
            return True, f"Branch {worktree_branch} not found — nothing to integrate."

    # 1. Try ff-only merge from worktree branch. Kept as a raw local-only merge
    # (NOT routed through vcs.submit(), which always pushes for mode="direct")
    # — the push/no-push submit() variant this eventually consolidates into is
    # decided in the vcs-route-integrate topic.
    result = subprocess.run(
        ["git", "-C", main_repo_path, "merge", "--ff-only", worktree_branch],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        left_at = f"Worktree left at {worktree_path}. " if worktree_path else ""
        return False, (
            f"Cannot ff-merge {worktree_branch} into main. "
            f"{left_at}Manual resolution required."
        )

    if not worktree_path:
        # No checkout directory to remove — branch-only cleanup (best-effort:
        # the merge already landed, so a failed delete here isn't fatal).
        subprocess.run(
            ["git", "-C", main_repo_path, "branch", "-d", worktree_branch],
            capture_output=True, text=True,
        )
        return True, f"Branch {worktree_branch} finalized (merged; no worktree_path recorded)."

    # 2+3. Remove worktree + delete branch (remove_workspace absorbs both).
    try:
        removed = backend_for(main_repo_path).remove_workspace(main_repo_path, worktree_path)
    except Exception as e:
        return False, f"Worktree/branch cleanup failed for {worktree_path}: {e}"
    if not removed:
        return False, f"Worktree/branch cleanup failed for {worktree_path}"

    return True, f"Worktree {worktree_path} finalized (merged {worktree_branch})."


def _main_worktree_root(repo_path: str) -> str:
    """Resolve ``repo_path`` to the MAIN worktree root.

    Critical for nested-dispatch safety: when an agent creates a worktree from
    *inside* another worktree (e.g. repo_path=/tmp/juggle-juggle-WR), deriving
    the path basename from that worktree compounds the name
    (juggle-juggle-juggle-WR-...) and the linked worktree may lack a main/master
    ref, breaking integrate. ``primary_root`` always resolves to the main
    worktree so basename is stable ("juggle") and ``create_workspace`` runs
    from the primary repo.
    """
    try:
        return backend_for(repo_path).primary_root(repo_path) or repo_path
    except Exception:
        return repo_path


def _create_worktree(
    repo_path: str, thread_label: str, worktree_root: str,
    *, db=None, topic_id: str | None = None,
) -> tuple[bool, str, str, str]:
    """Create an isolated git worktree for a thread.

    Returns (success, worktree_path, branch, message).
    worktree_path and branch are empty strings on failure.
    Idempotent: if worktree_path already exists, returns (True, path, branch, "already exists").

    ``worktree_root`` is REQUIRED (no default). A leaky ``= "/tmp"`` default
    once let a bare call write checkouts to /private/tmp outside pytest's
    tmp_path, accumulating 100+ orphaned dangling worktrees (2026-06-20). The
    production default now lives at the call site (``DEFAULT_WORKTREE_ROOT`` in
    juggle_dispatch_core), never here, so a parameter-less call fails loudly.

    ``db``/``topic_id`` (both optional, both-or-neither): when the thread is
    bound to a topic, the base is resolved via ``juggle_stack_base.stack_base``
    (H2 fix — forks the topic's stack-relative base, not the source repo's
    implicit HEAD). Without a topic binding, base falls back to
    ``backend.resolve(repo_path)`` — today's implicit-HEAD behavior, preserved
    verbatim for callers outside the topic/task-DAG flow.
    """
    repo_path = _main_worktree_root(repo_path)
    basename = Path(repo_path).name
    worktree_path = str(Path(worktree_root) / f"juggle-{basename}-{thread_label}")
    branch = f"cyc_{thread_label}"

    def _record_topic_branch() -> None:
        # T-fix-backfill-sha-misattribution: stamp the topic's OWN, durable
        # worktree_branch here — the single moment a topic's branch identity
        # is established — so later merged-sha proof never has to fall back
        # to a dispatch thread's live (reusable) worktree_branch field.
        if db is not None and topic_id is not None:
            from dbops.db_topics_worktree_branch import set_topic_worktree_branch
            set_topic_worktree_branch(db, topic_id, branch)

    if Path(worktree_path).exists():
        _register_worktree_trust(worktree_path)
        _record_topic_branch()
        return True, worktree_path, branch, f"Worktree already exists: {worktree_path}"

    try:
        backend = backend_for(repo_path)
    except Exception as e:
        return False, "", "", f"git worktree add failed: {e}"
    if db is not None and topic_id is not None:
        from juggle_stack_base import stack_base
        base = stack_base(db, topic_id, repo_path, backend)
    else:
        base = backend.resolve(repo_path)
    result = backend.create_workspace(repo_path, branch, worktree_path, base=base)
    if not result.ok:
        return False, "", "", f"git worktree add failed: {result.detail}"
    _record_topic_branch()

    # Symlink .venv for immediate test runs — skip silently when absent
    main_venv = Path(repo_path) / ".venv"
    worktree_venv = Path(worktree_path) / ".venv"
    if main_venv.exists() and not worktree_venv.exists():
        try:
            worktree_venv.symlink_to(main_venv)
        except OSError:
            pass

    # Pre-register the new dir as trusted so Claude Code doesn't prompt (bug E)
    _register_worktree_trust(worktree_path)

    return True, worktree_path, branch, f"Worktree created: {worktree_path} on branch {branch}"
