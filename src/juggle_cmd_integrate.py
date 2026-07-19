#!/usr/bin/env python3
"""Juggle — integrate command: rebase-aware atomic worktree finalization."""

import subprocess  # noqa: F401 — kept so tests can patch juggle_cmd_integrate.subprocess
import sys
from pathlib import Path

from dbops.graph_guards import is_agent_context
from juggle_settings import get_repo_config
from vcs import backend_for
from juggle_integrate_lock import (  # noqa: F401 — re-exported for callers
    AUTOPILOT_LOCK_TIMEOUT_SECS,
    INTEGRATE_LOCK_TIMEOUT_SECS,
    acquire_repo_lock,
    release_repo_lock,
)
from juggle_integrate_envelope import (
    FailContext, STEP_DIRTY_WORKTREE, STEP_EMPTY_BRANCH, STEP_NO_MAIN_BRANCH,
    STEP_REBASE_CONFLICT, STEP_TEST_FAILURE, STEP_UNEXPECTED,
    record_refusal, resolve_attempt_shas,
)
from juggle_integrate_diffstat import capture_diffstat


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

    # backend/rebase_onto set inside the try block below; pre-declared so _fail
    # can reference them (best-effort SHA capture) even when it's reached from
    # the catch-all except (e.g. backend_for() itself raised).
    backend = None
    rebase_onto = None

    def _fail(
        step: str, reason: str, *, files: list[str] | None = None, log_tail: str = "",
    ) -> tuple[bool, str]:
        trunk_sha, head_sha = resolve_attempt_shas(backend, main_repo_path, worktree_path, rebase_onto)
        ctx = FailContext(
            db=db, thread_id=thread_uuid, worktree_branch=worktree_branch,
            worktree_path=worktree_path, task=task, files=files or [],
            log_tail=log_tail, trunk_at_attempt=trunk_sha, head_at_attempt=head_sha,
        )
        record_refusal(step, reason, ctx)
        release_repo_lock(lock_path)
        return False, reason

    try:
        backend = backend_for(main_repo_path)

        # ── G1: Dirty-worktree gate (refuse; never auto-commit) ───────────────
        # Run FIRST — before any git side effects — so uncommitted work is never
        # silently destroyed by worktree cleanup on a subsequent path.
        dirty = backend.dirty_files(worktree_path)
        if dirty:
            n = len(dirty)
            return _fail(
                STEP_DIRTY_WORKTREE,
                f"integrate refused: uncommitted changes in {worktree_path} "
                f"({n} file(s)). Commit what should merge, or discard, then retry.\n"
                f"Files:\n" + "\n".join(dirty),
                files=dirty,
            )

        # ── 1. Refresh (non-fatal for repos without remotes) ──────────────────
        backend.refresh(main_repo_path)

        # ── 2. Determine rebase target ─────────────────────────────────────────
        # Stack-relative base (SPEC §4): a topic-bound thread rebases onto its
        # DERIVED dep topics' base (trunk when landed, dep tip when unlanded)
        # instead of always trunk — the second of the two stack_base insertion
        # points (the first is dispatch's create_workspace base).
        rebase_target = None
        if task and task.get("topic_id"):
            from juggle_stack_base import stack_base
            rebase_onto = stack_base(db, task["topic_id"], main_repo_path, backend)
        else:
            rebase_onto = backend.trunk(main_repo_path)
            # push_mode="none" NEVER pushes, so origin/main (trunk()'s usual
            # pick) never advances past its pre-existing state — rebasing/
            # merging onto it here would discard whatever a PRIOR sequential
            # integrate already merged into local main, only for that merge
            # to be silently reset away in submit()'s local-main sync
            # (2026-07-19 KF/KH/KG clobber incident: three sequential
            # push_mode=none integrates landed only the LAST). The actual git
            # rebase target is the TRUE CURRENT LOCAL main HEAD instead — it
            # already carries every prior local-only merge. `rebase_onto`
            # itself is left untouched: submit() still needs it (branch-name
            # shaped, e.g. "origin/main") for its checked-out-branch guard and
            # the final status message. Scoped to the plain (non-stack-based)
            # case only — a stacked/dependent topic's rebase target already
            # follows its own dependency-derived base, which this must not
            # override.
            if push_mode == "none":
                local_head = backend.resolve(main_repo_path)
                if local_head:
                    rebase_target = local_head
        if rebase_onto is None:
            return _fail(STEP_NO_MAIN_BRANCH, "Cannot determine main branch (no main/master ref found)")
        rebase_target = rebase_target or rebase_onto

        # ── 3. G2: Empty-branch guard ──────────────────────────────────────────
        if not backend.has_changes(worktree_path, since=rebase_onto):
            # G1 already ruled out dirty tree above, so the worktree is clean.
            # A clean tree with 0 commits ahead means no work was ever committed
            # on this branch.  Silently cleaning up here was the data-loss path
            # (2026-06-20: 857-line spec deleted when integrate ff-merged an
            # empty branch and the worktree cleanup deleted uncommitted files).
            # Refuse loudly so the agent can investigate and decide.
            return _fail(
                STEP_EMPTY_BRANCH,
                f"integrate refused: nothing to merge on {worktree_branch} "
                f"(0 commits ahead of {rebase_onto}). "
                f"Commit your work, or call complete-agent with ⚠️ PARTIAL/BLOCKER.",
            )

        # ── 4. Update the worktree onto the rebase target ──────────────────────
        # (idempotent in-progress-rebase recovery + merge.ours.driver setup for
        # graphify-out/ live inside update_to — see vcs_git.py.)
        upd = backend.update_to(worktree_path, rebase_target)
        if not upd.ok:
            conflict_files = "\n".join(upd.conflicts) if upd.conflicts else "(see git status)"
            return _fail(
                STEP_REBASE_CONFLICT,
                f"Rebase conflict on {worktree_branch} onto {rebase_onto}.\n"
                f"Conflicting files:\n{conflict_files}\n"
                f"Branch preserved at {worktree_path}. "
                f"Sequence this thread after the one writing those files, "
                f"or resolve manually and re-run `juggle integrate`.\n"
                f"NOTE: semantic line-conflicts are not auto-resolved — this is expected behavior.",
                files=upd.conflicts or [],
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
                return _fail(STEP_TEST_FAILURE, _reason, log_tail=_reason)

        # ── 5b. Graph-task diffstat capture (pre-merge, DA M4) ─────────────────
        capture_diffstat(db, backend, task, worktree_path, rebase_onto)

        # ── 5c. Version bump (P1, 2026-07-03): from branch commit prefixes,
        # post-diffstat pre-submit; no-op without plugin.json or feat/fix commits.
        from juggle_version_bump import apply_version_bump
        apply_version_bump(worktree_path, since=rebase_onto)

        # ── 6/7. Submit (pr → queue, direct/none → direct; "none" = no push).
        # ZA-incident guard, graphify-out discard, local-main-sync live in submit.
        submit_mode = "queue" if push_mode == "pr" else "direct"
        result = backend.submit(
            worktree_path, base=rebase_onto, mode=submit_mode,
            push=(push_mode == "direct"),
        )
        # ── 6b. Interpret the SubmitResult (failed/submitted/landed) + side
        # effects — extracted to juggle_integrate_submit (LOC gate, 2026-07-05);
        # the submitted arm additionally drives async-land publish.
        from juggle_integrate_submit import finalize_submit_result
        return finalize_submit_result(
            db, backend, result,
            thread_uuid=thread_uuid, worktree_path=worktree_path,
            worktree_branch=worktree_branch, main_repo_path=main_repo_path,
            rebase_onto=rebase_onto, rebase_target=rebase_target, push_mode=push_mode,
            fail=_fail, release=lambda: release_repo_lock(lock_path),
        )

    except Exception as e:
        return _fail(STEP_UNEXPECTED, f"Unexpected error during integrate: {e}", log_tail=str(e))


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
    # T-spool-10: integrate is watchdog-owned; agent-context direct calls refuse.
    if not getattr(args, "allow_legacy_agent_integrate", False) and is_agent_context():
        print("Error: integrate is watchdog-owned — direct agent-context calls are "
              "refused. Re-run with --allow-legacy-agent-integrate for an operator/legacy bypass.")
        sys.exit(1)
    allow_main = getattr(args, "allow_main", False)
    success, msg = _run_integrate(thread, db, allow_main=allow_main)
    if success:
        print(f"[juggle] integrate OK: {msg}")
    else:
        print(f"Error: integrate failed — {msg}")
        sys.exit(1)
