"""juggle_integrate_submit — interpret a backend SubmitResult into integrate's
terminal outcome + side effects.

Extracted from juggle_cmd_integrate (architecture LOC gate, 2026-07-05) and then
wired for async-land publish. Three arms:

  failed    -> fail() envelope (branch + worktree preserved, fail-closed).
  submitted -> async-land publish (SPEC 2026-07-05, Option B): when the backend is
               async_land-capable (Phabricator/Gerrit/Sapling — guard on the
               CAPABILITY, never the backend name), the diff/PR is PUBLISHED to an
               external land queue, not yet an ancestor of trunk. Record the
               ticket as the bound topic's submitted_rev and advance
               integrating->integrated-unlanded (NON-terminal, below verified —
               NEVER merged_sha/verified; the land poller confirms the real land
               and only then flips to verified). A synchronous git-pr backend
               (async_land=False) keeps its prior behaviour: push the branch for
               human review, no topic advance. Either way the worktree is torn
               down + the agent freed, but main_repo_path/worktree_branch are KEPT
               on the thread so the land poller can still resolve repo+ticket.
  landed    -> record merged_sha, teardown, self-repo daemon restart.

Must not own: the merge mechanics (vcs_git.submit), the topic state machine
(dbops.db_topics), or the integrate pipeline's gates (juggle_cmd_integrate).
``_record_merged_sha`` / ``_restart_juggle_daemons`` are re-read from
juggle_cmd_integrate at call time so the existing test patch surface
(``juggle_cmd_integrate._restart_juggle_daemons`` etc.) keeps working.
"""
from __future__ import annotations

from pathlib import Path

from juggle_repo_vcs import repo_async_land


def _advance_topic_submitted(db, thread_uuid, ticket) -> None:
    """Record ``ticket`` as the bound topic's submitted_rev and advance
    integrating->integrated-unlanded via mark_topic_completion (SPEC 2026-07-05).

    No-op for a legacy/non-topic thread (async-land is topic-scoped). Deliberately
    NOT fail-soft: a raise propagates to _run_integrate's catch-all, which files a
    fail envelope and PRESERVES the worktree/branch (detect-refuse-preserve) rather
    than tearing down and dropping the ticket — the exact 2026-07-05 wedge.

    Loop re-fire (OUT OF SCOPE, SPEC deferred): P3a's four-seam reset would clear
    submitted_rev mid-flight if an async-land topic belonged to a re-firing loop.
    No async-land Meta loops exist yet — add a re-fire guard only when they do
    (repo TODO.md 'Deferred')."""
    from dbops import db_topics

    topic = db_topics.get_topic_by_thread(db, thread_uuid)
    if not topic:
        return
    db_topics.mark_topic_completion(
        db, topic["id"], integrate_ok=True, verify_ok=True, submitted_rev=ticket,
    )


def finalize_submit_result(db, backend, result, *, thread_uuid, worktree_path,
                           worktree_branch, main_repo_path, rebase_onto,
                           push_mode, fail, release, task=None):
    """Interpret ``result`` (a vcs_types.SubmitResult) into ``(ok, msg)`` plus its
    side effects. ``fail`` is _run_integrate's refusal closure (files a fail
    envelope + releases the lock); ``release`` releases the merge-queue lock.
    ``task`` is the graph-task binding (None for an ad-hoc thread) — a landed
    ad-hoc integrate additionally reconciles the conversation node's
    background-wedge (2026-07-07 #5558/#5564; see juggle_topic_lifecycle
    .reconcile_adhoc_integrate)."""
    from juggle_integrate_envelope import STEP_SUBMIT_FAILED
    from juggle_cmd_integrate import _record_merged_sha, _restart_juggle_daemons

    if result.status == "failed":
        return fail(STEP_SUBMIT_FAILED, result.detail, log_tail=result.detail)

    if result.status == "submitted":
        async_land = repo_async_land(main_repo_path, backend)
        ticket = result.ticket or worktree_branch
        if async_land:
            # Publish advances the topic to 'integrated-unlanded' (the land poller
            # promotes to verified later). Runs BEFORE teardown so a failure
            # preserves the worktree instead of dropping the ticket.
            _advance_topic_submitted(db, thread_uuid, ticket)
        # Worktree torn down + agent freed; main_repo_path + worktree_branch KEPT
        # so the land poller (async) / the PR (git-pr) can still resolve the work.
        backend.remove_workspace(main_repo_path, worktree_path)
        db.update_thread(thread_uuid, worktree_path="", worktree_branch=worktree_branch,
                         main_repo_path=main_repo_path)
        release()
        if async_land:
            return True, (f"Topic submitted to async land queue (ticket {ticket}); "
                          f"worktree freed, awaiting land confirmation")
        return True, f"Branch {worktree_branch} pushed to origin for PR (no local merge)"

    # status == "landed": direct/none — record merged_sha, clean up.
    # Recorded AFTER the push (defect C, 2026-07-01): _record_merged_sha
    # checks ancestry against canonical origin/<main>, so recording BEFORE
    # the push tested against an origin/<main> that did not yet contain the
    # commit → merged_sha left NULL and the topic wedged at 'integrating'.
    # Still BEFORE the worktree fields are cleared below (thread → topic
    # binding still resolves).
    _record_merged_sha(db, thread_uuid, main_repo_path, result.landed_rev)

    # Ad-hoc (no graph task bound) thread: _record_merged_sha's topic lookup
    # is a no-op (no kind='topic' node), so nothing else reconciles the
    # conversation node's 'background' state — do it here (2026-07-07
    # #5558/#5564), BEFORE the worktree fields are cleared below, same as the
    # merged_sha recording above.
    if task is None:
        from juggle_topic_lifecycle import reconcile_adhoc_integrate
        reconcile_adhoc_integrate(db, thread_uuid, result.landed_rev)

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
