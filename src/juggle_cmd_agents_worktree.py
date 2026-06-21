"""
juggle_cmd_agents_worktree — Git worktree helpers for agent dispatch/completion.

Owns: _create_worktree (isolated worktree per thread, used by send-task) and
      _finalize_worktree (ff-merge → remove → branch-delete, used by complete-agent).
Must not own: command handler logic or DB access.
"""

import subprocess
from pathlib import Path


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
    """
    worktree_path = (thread.get("worktree_path") or "").strip()
    worktree_branch = (thread.get("worktree_branch") or "").strip()
    main_repo_path = (thread.get("main_repo_path") or "").strip()

    if not worktree_path or not worktree_branch or not main_repo_path:
        return True, ""  # No worktree to finalize

    if not Path(worktree_path).exists():
        return True, f"Worktree already removed: {worktree_path}"

    if not Path(main_repo_path).exists():
        return False, f"Main repo not found: {main_repo_path}"

    # 1. Try ff-only merge from worktree branch
    result = subprocess.run(
        ["git", "-C", main_repo_path, "merge", "--ff-only", worktree_branch],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, (
            f"Cannot ff-merge {worktree_branch} into main. "
            f"Worktree left at {worktree_path}. Manual resolution required."
        )

    # 2. Remove worktree
    result = subprocess.run(
        ["git", "-C", main_repo_path, "worktree", "remove", worktree_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, f"Worktree remove failed: {result.stderr.strip()}"

    # 3. Delete branch
    result = subprocess.run(
        ["git", "-C", main_repo_path, "branch", "-d", worktree_branch],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return True, f"Merged + worktree removed, but branch delete failed: {result.stderr.strip()}"

    return True, f"Worktree {worktree_path} finalized (merged {worktree_branch})."


def _main_worktree_root(repo_path: str) -> str:
    """Resolve ``repo_path`` to the MAIN worktree root.

    Critical for nested-dispatch safety: when an agent creates a worktree from
    *inside* another worktree (e.g. repo_path=/tmp/juggle-juggle-WR), deriving
    the path basename from that worktree compounds the name
    (juggle-juggle-juggle-WR-...) and the linked worktree may lack a main/master
    ref, breaking integrate. The first entry of ``git worktree list --porcelain``
    is always the main worktree; use its path so basename is stable ("juggle")
    and ``git worktree add`` runs from the primary repo.
    """
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("worktree "):
                    return line[len("worktree "):].strip()
    except Exception:
        pass
    return repo_path


def _create_worktree(
    repo_path: str, thread_label: str, worktree_root: str
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
    """
    repo_path = _main_worktree_root(repo_path)
    basename = Path(repo_path).name
    worktree_path = str(Path(worktree_root) / f"juggle-{basename}-{thread_label}")
    branch = f"cyc_{thread_label}"

    if Path(worktree_path).exists():
        _register_worktree_trust(worktree_path)
        return True, worktree_path, branch, f"Worktree already exists: {worktree_path}"

    result = subprocess.run(
        ["git", "-C", repo_path, "worktree", "add", "-b", branch, worktree_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, "", "", f"git worktree add failed: {result.stderr.strip()}"

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
