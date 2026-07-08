"""juggle_brief_git — best-effort git/worktree facts for the brief command.

Isolated from ``juggle_brief_collect`` along the git-IO domain seam (and to keep
each module under the architecture LOC gate). Every function is best-effort: a
missing/broken repo degrades to a default, never raises — brief must render even
when git is unavailable.
"""
from __future__ import annotations

import subprocess


def run_git(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def commits_ahead(path: str, trunk: str) -> int:
    out = run_git(["git", "-C", path, "rev-list", "--count", f"{trunk}..HEAD"])
    try:
        return int(out or "0")
    except ValueError:
        return 0


def _list_worktrees(main: str) -> list[tuple[str, str]]:
    out = run_git(["git", "-C", main, "worktree", "list", "--porcelain"])
    entries: list[tuple[str, str]] = []
    path, branch = "", ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            if path:
                entries.append((path, branch))
            path, branch = line[len("worktree "):].strip(), ""
        elif line.startswith("branch "):
            branch = line[len("branch "):].strip().split("/")[-1]
    if path:
        entries.append((path, branch))
    return entries


def git_health(active_threads: list[dict], fallback_repo: str) -> dict:
    """Main-repo branch/dirty + worktree clean/unmerged/orphan tally.

    A worktree is ORPHAN when its branch is not the branch of any active topic,
    UNMERGED when it has commits ahead of trunk (or is dirty), else CLEAN. Best-
    effort — any git failure degrades the affected field to a zero/'?'."""
    from vcs import backend_for

    bound = {
        (t.get("worktree_branch") or "").strip()
        for t in active_threads
        if (t.get("worktree_branch") or "").strip()
    }
    repo = ""
    for t in active_threads:
        repo = (t.get("main_repo_path") or "").strip()
        if repo:
            break
    repo = repo or fallback_repo

    branch, dirty = "?", False
    worktrees = {"clean": 0, "unmerged": 0, "orphan": 0}
    try:
        backend = backend_for(repo)
        main = backend.primary_root(repo)
        branch = run_git(["git", "-C", main, "symbolic-ref", "--short", "HEAD"]) or "?"
        dirty = bool(backend.dirty_files(main))
        trunk = backend.trunk(main) or "HEAD"
        for path, wbranch in _list_worktrees(main)[1:]:  # [0] is the main worktree
            ahead = commits_ahead(path, trunk)
            wdirty = bool(run_git(["git", "-C", path, "status", "--porcelain"]))
            if wbranch and wbranch not in bound:
                worktrees["orphan"] += 1
            elif ahead > 0 or wdirty:
                worktrees["unmerged"] += 1
            else:
                worktrees["clean"] += 1
    except Exception:
        pass
    return {"git_branch": branch, "dirty": dirty, "worktrees": worktrees}


def worktree_facts(thread: dict) -> dict:
    """Per-topic worktree facts: branch, exists, dirty, unmerged_commits."""
    from pathlib import Path

    path = (thread.get("worktree_path") or "").strip()
    branch = (thread.get("worktree_branch") or "").strip()
    exists = bool(path) and Path(path).exists()
    dirty, unmerged = False, 0
    if exists:
        try:
            from vcs import backend_for

            main = (thread.get("main_repo_path") or "").strip() or path
            trunk = backend_for(main).trunk(main) or "HEAD"
            unmerged = commits_ahead(path, trunk)
            dirty = bool(run_git(["git", "-C", path, "status", "--porcelain"]))
        except Exception:
            pass
    return {
        "branch": branch or "-",
        "exists": exists,
        "dirty": dirty,
        "unmerged_commits": unmerged,
        "tests_green": None,
    }
