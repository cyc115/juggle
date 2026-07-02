#!/usr/bin/env python3
"""Juggle — integrate command: rebase-aware atomic worktree finalization."""

import subprocess
import sys
from pathlib import Path

from juggle_settings import get_repo_config
from juggle_integrate_lock import (  # noqa: F401 — re-exported for callers
    AUTOPILOT_LOCK_TIMEOUT_SECS,
    INTEGRATE_LOCK_TIMEOUT_SECS,
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


# Source-binding guard lives in juggle_repo_binding (single source of truth,
# shared with agent spawn). Re-exported for the existing test import surface.
from juggle_repo_binding import (  # noqa: E402,F401
    assert_source_binding as _assert_source_binding,
    canonical_main_ref as _canonical_main_ref,
)


# Merged-SHA recorder lives in juggle_integrate_mergedsha (loc_gate budget);
# re-exported here so the existing juggle_cmd_integrate._record_merged_sha
# import/patch surface keeps working.
from juggle_integrate_mergedsha import _record_merged_sha  # noqa: E402,F401


# ── Self-repo daemon restart (juggle_integrate_selfrepo; name kept here so
# tests patching juggle_cmd_integrate._restart_juggle_daemons keep working) ──

from juggle_integrate_selfrepo import _restart_juggle_daemons  # noqa: E402,F401


# ── Pre-merge guard helpers (pure where possible; tested independently) ───────


def is_worktree_dirty(worktree_path: str) -> bool:
    """Return True if the worktree has any uncommitted changes (staged or unstaged)."""
    result = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def branch_commits_ahead(repo_path: str, branch: str, target: str) -> int:
    """Return the number of commits on *branch* not yet in *target*; -1 on error."""
    result = subprocess.run(
        ["git", "-C", repo_path, "rev-list", "--count", f"{target}..{branch}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return -1
    return int(result.stdout.strip() or "0")


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

    # Graph-task binding still drives the pre-merge verify gate below; it no
    # longer selects the lock deadline (see lock_timeout).
    task = _graph_task_for_thread(db, thread_uuid)

    # Source-binding guard (2026-06-16 multi-repo incident): an autopilot topic
    # mis-bound to ~/.claude would ff-merge an empty branch (work dropped) or
    # push the wrong HEAD. Refuse BEFORE any git side effects / lock acquisition.
    bind_err = _assert_source_binding(main_repo_path, is_autopilot=bool(task))
    if bind_err:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ integrate refused [{worktree_branch}]: {bind_err}",
            type_="manual_step",
            priority="high",
        )
        return False, bind_err

    # Global serialized integrate lock (#5038): acquire the per-repo merge-queue
    # lock HERE — before fetch/rebase/suite/merge/push — so the ENTIRE integrate
    # (including the full suite) runs under it. Acquisition BLOCKS uniformly with
    # the 1800s safety valve (no 300s fast-fail); waiters queue behind the holder
    # and win when it releases, so only one integrate/suite runs at a time.
    try:
        lock_path = acquire_repo_lock(
            main_repo_path, timeout_secs=INTEGRATE_LOCK_TIMEOUT_SECS
        )
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
        # ── G1: Dirty-worktree gate (refuse; never auto-commit) ───────────────
        # Run FIRST — before any git side effects — so uncommitted work is never
        # silently destroyed by worktree cleanup on a subsequent path.
        if is_worktree_dirty(worktree_path):
            dirty_files = subprocess.run(
                ["git", "-C", worktree_path, "status", "--porcelain"],
                capture_output=True, text=True,
            ).stdout.strip()
            n = len(dirty_files.splitlines())
            return _fail(
                f"integrate refused: uncommitted changes in {worktree_path} "
                f"({n} file(s)). Commit what should merge, or discard, then retry.\n"
                f"Files:\n{dirty_files}"
            )

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

        # ── 3. G2: Empty-branch guard ─────────────────────────────────────────
        # Use branch_commits_ahead; fall back to 1 so an error doesn't block a
        # real branch (conservative: assume work exists when count is unknown).
        ahead_count = branch_commits_ahead(main_repo_path, worktree_branch, rebase_onto)
        if ahead_count < 0:
            ahead_count = 1  # unknown — assume work exists, proceed to rebase

        if ahead_count == 0:
            # G1 already ruled out dirty tree above, so the worktree is clean.
            # A clean tree with 0 commits ahead means no work was ever committed
            # on this branch.  Silently cleaning up here was the data-loss path
            # (2026-06-20: 857-line spec deleted when integrate ff-merged an
            # empty branch and the worktree cleanup deleted uncommitted files).
            # Refuse loudly so the agent can investigate and decide.
            return _fail(
                f"integrate refused: nothing to merge on {worktree_branch} "
                f"(0 commits ahead of {rebase_onto}). "
                f"Commit your work, or call complete-agent with ⚠️ PARTIAL/BLOCKER."
            )

        # ── 3b. Ensure graphify-out can never block the rebase/merge ──────────
        # Every coder branch regenerates graphify-out/ (the ~3580-line graph.json
        # + manifest), so two branches conflict unmergeably on it (2026-06-21
        # concurrent-integrate pileup, root cause 2). `.gitattributes` routes
        # graphify-out/** to `merge=ours`, but that driver is LOCAL git config —
        # set it idempotently here so the integrate flow is self-sufficient even
        # in a repo/worktree where install-graphify-hooks.sh never ran. The
        # `true` driver keeps the in-progress side (graphify regenerates anyway).
        subprocess.run(
            ["git", "-C", main_repo_path, "config", "merge.ours.driver", "true"],
            capture_output=True, text=True,
        )

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

        # ── 5. Run the FULL test suite (only when test_cmd set AND push != none) ─
        # Directive (2026-06-20): integrate ALWAYS runs the FULL suite verbatim —
        # never a subset. B2 (2026-06-21): the runner additionally REFUSES (fail
        # loud, not munging) a test_cmd that would subset the suite (e.g.
        # `-m 'not slow'`, `--deselect`). One retry on flake. (Removed: test_scope
        # / quarantine_tests branches + juggle_integrate_testscope import.)
        if test_cmd and push_mode != "none":
            from juggle_integrate_fullsuite import run_test_cmd_full
            _ok, _reason = run_test_cmd_full(test_cmd, worktree_path, worktree_branch)
            if not _ok:
                return _fail(_reason)

        # ── 5b. Graph-task diffstat capture (pre-merge, DA M4) — only cheap
        # moment: integrate deletes the branch+worktree on success. Best-effort.
        if task:
            try:
                from dbops import db_graph
                _diff = subprocess.run(
                    ["git", "-C", worktree_path, "diff", "--stat", f"{rebase_onto}..HEAD"],
                    capture_output=True, text=True,
                )
                diffstat = (_diff.stdout or "").strip()[:2000] if _diff.returncode == 0 else ""
                if diffstat:
                    db_graph.set_task_diffstat(db, task["id"], diffstat)
            except Exception:
                pass  # diffstat is best-effort hydration enrichment, never a gate

        # ── 6. Resolve local main branch name + validate HEAD ────────────────
        local_main = subprocess.run(
            ["git", "-C", main_repo_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip() or "main"

        # Derive expected branch name from the rebase target (origin/main →
        # main, origin/master → master, etc.) and fail loudly if the main
        # working tree is on the wrong branch. This catches external state
        # where main was left checked out on a feature branch (2026-06-14 ZA
        # incident) before silently merging into or pushing the wrong branch.
        expected_main = rebase_onto.split("/")[-1]  # "origin/main" → "main"
        if local_main != expected_main:
            return _fail(
                f"main_repo_path HEAD is on '{local_main}', expected '{expected_main}'. "
                f"Check out '{expected_main}' in {main_repo_path} and re-run integrate."
            )

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
        # bug G). Also clean untracked files: if the feature branch adds a NEW
        # graphify-out/ file (e.g. .graphify_chunk_03.json) and graphify watch
        # has already written it as an untracked file in main, git merge
        # --ff-only fails with "untracked working tree file would be overwritten"
        # — git checkout -- does NOT remove untracked files (2026-06-14 bug).
        # Discarding is safe: graphify regenerates all files on demand.
        if Path(main_repo_path, "graphify-out").exists():
            subprocess.run(
                ["git", "-C", main_repo_path, "checkout", "--", "graphify-out/"],
                capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", main_repo_path, "clean", "-fd", "--", "graphify-out/"],
                capture_output=True, text=True,
            )

        # ── 6b. Sync local main to the rebase base BEFORE the ff-merge ────────
        # The branch was rebased onto `rebase_onto`, but LOCAL main can have
        # drifted from it — a concurrent integrate advanced/pushed main, or an
        # un-pushed/aborted commit left main diverged — so `merge --ff-only
        # <branch>` aborts "Not possible to fast-forward" (2026-06-21
        # concurrent-integrate pileup, root cause 1). Make local main EXACTLY the
        # rebase base so the rebased branch always fast-forwards. A plain
        # `merge --ff-only <base>` is insufficient: a local main that is AHEAD of
        # the base (an un-pushed/aborted commit) reports "already up to date" yet
        # still diverges from the branch — so hard-reset to the base. Guard:
        # never discard uncommitted tracked work (fail loud / forward-only FF).
        tracked_dirty = subprocess.run(
            ["git", "-C", main_repo_path, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True,
        ).stdout.strip()
        if tracked_dirty:
            # Can't safely hard-reset. A forward-only FF handles the common
            # "behind" race without touching uncommitted files; fail loud if the
            # local main has actually diverged from the base.
            sync = subprocess.run(
                ["git", "-C", main_repo_path, "merge", "--ff-only", rebase_onto],
                capture_output=True, text=True,
            )
            if sync.returncode != 0:
                return _fail(
                    f"local main diverged from {rebase_onto} with uncommitted tracked "
                    f"changes in {main_repo_path}; resolve manually then re-run integrate."
                )
        else:
            subprocess.run(
                ["git", "-C", main_repo_path, "reset", "--hard", rebase_onto],
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

        # Record the merged commit (local main tip == branch tip) as the topic's
        # merged_sha — the single source of truth for the verified gate. Recorded
        # AFTER the push (defect C, 2026-07-01): _record_merged_sha checks ancestry
        # against canonical origin/<main>, so recording BEFORE the push tested
        # against an origin/<main> that did not yet contain the commit → merged_sha
        # left NULL and the topic wedged at 'integrating'. Still BEFORE the worktree
        # fields are cleared below (thread → topic binding still resolves).
        _record_merged_sha(db, thread_uuid, main_repo_path, local_main)

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
