#!/usr/bin/env python3
"""Juggle — integrate command: rebase-aware atomic worktree finalization."""

import subprocess
import sys
from pathlib import Path

from juggle_settings import get_repo_config
from juggle_integrate_lock import (  # noqa: F401 — re-exported for callers
    AUTOPILOT_LOCK_TIMEOUT_SECS,
    acquire_repo_lock,
    release_repo_lock,
)


def _graph_task_for_thread(db, thread_uuid: str) -> dict | None:
    """Graph task bound to this thread, or None (pre-migration DB, Mock db)."""
    try:
        from dbops import db_graph
        return db_graph.get_task_by_thread(db, thread_uuid)
    except Exception:
        return None


# ── Self-repo daemon restart (juggle_integrate_selfrepo; name kept here so
# tests patching juggle_cmd_integrate._restart_juggle_daemons keep working) ──

from juggle_integrate_selfrepo import _restart_juggle_daemons  # noqa: E402,F401


# ── Core integration pipeline ─────────────────────────────────────────────────

def _run_integrate(thread: dict, db, allow_main: bool = False) -> tuple[bool, str]:
    """Atomic fetch → rebase → test → ff-merge → push → cleanup for a worktree.

    Fail-closed: rebase conflict or test failure → action_item, branch+worktree preserved.
    Idempotent:
      - In-progress rebase aborted on entry before retrying.
      - Branch 0 commits ahead of main → skip merge, go straight to cleanup.
    push_mode controls post-merge: direct=push main, pr=push branch only, none=local only.
    """
    worktree_path = (thread.get("worktree_path") or "").strip()
    worktree_branch = (thread.get("worktree_branch") or "").strip()
    main_repo_path = (thread.get("main_repo_path") or "").strip()
    thread_uuid = thread.get("id", "")

    if not worktree_path or not worktree_branch or not main_repo_path:
        return False, "Missing worktree fields — nothing to integrate"

    if not Path(worktree_path).exists():
        return False, f"Worktree path does not exist: {worktree_path}"

    repo_cfg = get_repo_config(main_repo_path)
    push_mode = repo_cfg["push_mode"]
    test_cmd = repo_cfg["test_cmd"]

    # Autopilot context (thread bound to a graph task): fan-in completions
    # legitimately queue behind a long test_cmd — wait up to 30 min (DA M2).
    task = _graph_task_for_thread(db, thread_uuid)
    lock_timeout = AUTOPILOT_LOCK_TIMEOUT_SECS if task else 300.0
    try:
        lock_path = acquire_repo_lock(main_repo_path, timeout_secs=lock_timeout)
    except RuntimeError as e:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ integrate lock timeout [{worktree_branch}]: {e}",
            type_="manual_step",
            priority="high",
        )
        return False, f"Lock acquisition failed: {e}"

    def _fail(reason: str) -> tuple[bool, str]:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ integrate failed [{worktree_branch}]: {reason}",
            type_="manual_step",
            priority="high",
        )
        release_repo_lock(lock_path)
        return False, reason

    try:
        # ── 0. Abort any in-progress rebase (idempotency) ────────────────────
        git_dir_result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--git-dir"],
            capture_output=True, text=True,
        )
        if git_dir_result.returncode == 0:
            gd = git_dir_result.stdout.strip()
            git_dir = gd if Path(gd).is_absolute() else str(Path(worktree_path) / gd)
            if Path(git_dir, "rebase-merge").exists() or Path(git_dir, "rebase-apply").exists():
                subprocess.run(
                    ["git", "-C", worktree_path, "rebase", "--abort"],
                    capture_output=True, text=True,
                )

        # ── 1. Fetch (non-fatal for repos without remotes) ───────────────────
        subprocess.run(
            ["git", "-C", main_repo_path, "fetch", "--prune"],
            capture_output=True, text=True,
        )

        # ── 2. Determine rebase target ────────────────────────────────────────
        rebase_onto = None
        for candidate in ("origin/main", "origin/master", "main", "master"):
            if subprocess.run(
                ["git", "-C", main_repo_path, "rev-parse", "--verify", candidate],
                capture_output=True, text=True,
            ).returncode == 0:
                rebase_onto = candidate
                break
        if rebase_onto is None:
            return _fail("Cannot determine main branch (no main/master ref found)")

        # ── 3. Idempotency: already merged? skip to cleanup ───────────────────
        ahead_result = subprocess.run(
            ["git", "-C", main_repo_path, "rev-list", "--count",
             f"{rebase_onto}..{worktree_branch}"],
            capture_output=True, text=True,
        )
        ahead_count = (
            int(ahead_result.stdout.strip() or "0")
            if ahead_result.returncode == 0 else 1
        )

        if ahead_count == 0:
            subprocess.run(
                ["git", "-C", main_repo_path, "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", main_repo_path, "branch", "-D", worktree_branch],
                capture_output=True, text=True,
            )
            db.update_thread(thread_uuid, worktree_path="", worktree_branch="", main_repo_path="")
            release_repo_lock(lock_path)
            return True, f"Branch {worktree_branch} already merged into {rebase_onto} — cleaned up."

        # ── 4. Rebase ─────────────────────────────────────────────────────────
        result = subprocess.run(
            ["git", "-C", worktree_path, "rebase", rebase_onto],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            conflicts_result = subprocess.run(
                ["git", "-C", worktree_path, "diff", "--name-only", "--diff-filter=U"],
                capture_output=True, text=True,
            )
            conflict_files = conflicts_result.stdout.strip() or "(see git status)"
            subprocess.run(
                ["git", "-C", worktree_path, "rebase", "--abort"],
                capture_output=True, text=True,
            )
            return _fail(
                f"Rebase conflict on {worktree_branch} onto {rebase_onto}.\n"
                f"Conflicting files:\n{conflict_files}\n"
                f"Branch preserved at {worktree_path}. "
                f"Sequence this thread after the one writing those files, "
                f"or resolve manually and re-run `juggle integrate`.\n"
                f"NOTE: semantic line-conflicts are not auto-resolved — this is expected behavior."
            )

        # ── 5. Run test_cmd (only when configured AND push_mode != none) ──────
        if test_cmd and push_mode != "none":
            result = subprocess.run(
                test_cmd, shell=True, capture_output=True, text=True, cwd=worktree_path,
            )
            if result.returncode != 0:
                return _fail(
                    f"Tests failed (exit {result.returncode}) for {worktree_branch}. "
                    f"No merge performed. "
                    f"stdout tail: {result.stdout[-300:].strip()}"
                )

        # ── 5b. Graph-task gate: pre-merge diffstat + verify_cmd (DA M3) ──────
        # Runs in the worktree, post-rebase, BEFORE any merge/push — alongside
        # test_cmd (both must pass if both set). A verify failure aborts here:
        # main untouched, worktree + branch preserved, task → failed-verify.
        from juggle_integrate_verify import verify_task_premerge
        v_ok, v_reason = verify_task_premerge(db, task, worktree_path, rebase_onto)
        if not v_ok:
            return _fail(v_reason)

        # ── 6. Resolve local main branch name ────────────────────────────────
        local_main = subprocess.run(
            ["git", "-C", main_repo_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip() or "main"

        # ── 7. Merge + push (mode-dependent) ─────────────────────────────────
        if push_mode == "pr":
            # Push feature branch to origin; do NOT ff-merge local main
            push_result = subprocess.run(
                ["git", "-C", worktree_path, "push", "origin",
                 f"{worktree_branch}:{worktree_branch}", "--force-with-lease"],
                capture_output=True, text=True,
            )
            if push_result.returncode != 0:
                return _fail(f"Push branch for PR failed: {push_result.stderr.strip()}")
            # Remove worktree; leave branch ref on remote for PR
            subprocess.run(
                ["git", "-C", main_repo_path, "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True,
            )
            db.update_thread(thread_uuid, worktree_path="", worktree_branch=worktree_branch,
                             main_repo_path=main_repo_path)
            release_repo_lock(lock_path)
            return True, f"Branch {worktree_branch} pushed to origin for PR (no local merge)"

        # Discard any local modifications to graphify-out/ before the ff-merge.
        # The graphify watch hook regenerates tracked files in graphify-out/ on
        # every commit; if the agent's branch also updated them, git merge
        # --ff-only fails with "local changes would be overwritten" (2026-06-11
        # bug G). Discarding is safe: graphify regenerates them on demand.
        if Path(main_repo_path, "graphify-out").exists():
            subprocess.run(
                ["git", "-C", main_repo_path, "checkout", "--", "graphify-out/"],
                capture_output=True, text=True,
            )

        # direct or none: ff-merge into local main
        result = subprocess.run(
            ["git", "-C", main_repo_path, "merge", "--ff-only", worktree_branch],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return _fail(f"FF-merge of {worktree_branch} failed: {result.stderr.strip()}")

        if push_mode == "direct":
            push_result = subprocess.run(
                ["git", "-C", main_repo_path, "push", "origin",
                 f"{local_main}:{local_main}"],
                capture_output=True, text=True,
            )
            if push_result.returncode != 0:
                return _fail(f"Push failed: {push_result.stderr.strip()}")

        # ── 8. Remove worktree + branch ───────────────────────────────────────
        subprocess.run(
            ["git", "-C", main_repo_path, "worktree", "remove", "--force", worktree_path],
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", main_repo_path, "branch", "-d", worktree_branch],
            capture_output=True, text=True,
        )

        # ── 9. Clear worktree fields on thread ────────────────────────────────
        db.update_thread(thread_uuid, worktree_path="", worktree_branch="", main_repo_path="")

        # ── 10. Self-repo: restart watchdog + monitor ─────────────────────────
        from juggle_cli_common import SRC_DIR as _SRC_DIR
        juggle_own_repo = str(Path(_SRC_DIR).parent.resolve())
        if Path(main_repo_path).resolve() == Path(juggle_own_repo).resolve():
            _restart_juggle_daemons()

        release_repo_lock(lock_path)
        return True, f"Integrated {worktree_branch} → {local_main} (push_mode={push_mode})"

    except Exception as e:
        return _fail(f"Unexpected error during integrate: {e}")


# ── CLI imports needed by cmd_integrate ──────────────────────────────────────

def _resolve_thread(db, thread_id: str) -> str:
    from juggle_cli_common import _resolve_thread as _rt
    return _rt(db, thread_id)


def get_db():
    from juggle_cli_common import get_db as _get_db
    return _get_db()


# ── CLI entry point ───────────────────────────────────────────────────────────

def cmd_integrate(args):
    """juggle integrate <thread> — rebase-aware atomic worktree finalization."""
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    allow_main = getattr(args, "allow_main", False)
    success, msg = _run_integrate(thread, db, allow_main=allow_main)

    if success:
        print(f"[juggle] integrate OK: {msg}")
    else:
        print(f"Error: integrate failed — {msg}")
        sys.exit(1)
