"""juggle_integrate_submit — interpret a backend SubmitResult into integrate's
terminal outcome + side effects.

Extracted from juggle_cmd_integrate (architecture LOC gate, 2026-07-05) as a
behaviour-preserving split: juggle_cmd_integrate sat at its ~300-line budget, so
the three-arm SubmitResult interpreter (failed / submitted / landed) moves here
BEFORE the async-land wiring is added on top.

Three arms:
  failed    -> fail() envelope (branch + worktree preserved, fail-closed).
  submitted -> async-land publish (SPEC 2026-07-05): when the backend is
               async_land-capable, the diff/PR is PUBLISHED to an external land
               queue (not yet an ancestor of trunk). Record the ticket as the
               topic's submitted_rev and advance integrating->integrated-unlanded
               (NON-terminal, below verified — NEVER merged_sha/verified; the land
               poller confirms the real land and only then flips to verified).
               A synchronous git-pr backend (async_land=False) keeps its prior
               behaviour: push the branch for review, no topic advance. Either
               way the worktree is torn down and the agent freed, keeping
               main_repo_path so the land poller can still resolve repo+ticket.
  landed    -> record merged_sha, teardown, self-repo daemon restart.

Must not own: the merge mechanics (vcs_git.submit), the topic state machine
(dbops.db_topics), or the integrate pipeline's gates (juggle_cmd_integrate).
``_record_merged_sha`` / ``_restart_juggle_daemons`` are re-read from
juggle_cmd_integrate at call time so the existing test patch surface
(``juggle_cmd_integrate._restart_juggle_daemons`` etc.) keeps working.
"""
from __future__ import annotations

from pathlib import Path


def finalize_submit_result(db, backend, result, *, thread_uuid, worktree_path,
                           worktree_branch, main_repo_path, rebase_onto,
                           push_mode, fail, release):
    """Interpret ``result`` (a vcs_types.SubmitResult) into ``(ok, msg)`` plus its
    side effects. ``fail`` is _run_integrate's refusal closure (files a fail
    envelope + releases the lock); ``release`` releases the merge-queue lock."""
    from juggle_integrate_envelope import STEP_SUBMIT_FAILED
    from juggle_cmd_integrate import _record_merged_sha, _restart_juggle_daemons

    if result.status == "failed":
        return fail(STEP_SUBMIT_FAILED, result.detail, log_tail=result.detail)

    if result.status == "submitted":
        # PR mode: worktree removed, branch ref left for the PR — main
        # untouched, local main branch/repo binding kept on the thread.
        backend.remove_workspace(main_repo_path, worktree_path)
        db.update_thread(thread_uuid, worktree_path="", worktree_branch=worktree_branch,
                         main_repo_path=main_repo_path)
        release()
        return True, f"Branch {worktree_branch} pushed to origin for PR (no local merge)"

    # status == "landed": direct/none — record merged_sha, clean up.
    # Recorded AFTER the push (defect C, 2026-07-01): _record_merged_sha
    # checks ancestry against canonical origin/<main>, so recording BEFORE
    # the push tested against an origin/<main> that did not yet contain the
    # commit → merged_sha left NULL and the topic wedged at 'integrating'.
    # Still BEFORE the worktree fields are cleared below (thread → topic
    # binding still resolves).
    _record_merged_sha(db, thread_uuid, main_repo_path, result.landed_rev)

    # Remove worktree + branch, then clear worktree fields on the thread.
    backend.remove_workspace(main_repo_path, worktree_path)
    db.update_thread(thread_uuid, worktree_path="", worktree_branch="", main_repo_path="")

    # Self-repo: restart watchdog + monitor.
    from juggle_cli_common import SRC_DIR as _SRC_DIR
    juggle_own_repo = str(Path(_SRC_DIR).parent.resolve())
    if Path(main_repo_path).resolve() == Path(juggle_own_repo).resolve():
        _restart_juggle_daemons()

    release()
    local_main = rebase_onto.split("/")[-1]
    return True, f"Integrated {worktree_branch} → {local_main} (push_mode={push_mode})"
