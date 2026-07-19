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
from vcs_types import _run


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


def _commit_count(repo: str, since: str, until: str) -> int:
    """``git rev-list --count since..until`` — best-effort (0 on any git error),
    used only for the human-facing landed notification, never a gate."""
    out = _run(["git", "-C", repo, "rev-list", "--count", f"{since}..{until}"], repo)
    try:
        return int(out or "0")
    except (TypeError, ValueError):
        return 0


def finalize_submit_result(db, backend, result, *, thread_uuid, worktree_path,
                           worktree_branch, main_repo_path, rebase_onto,
                           rebase_target=None, push_mode, fail, release):
    """Interpret ``result`` (a vcs_types.SubmitResult) into ``(ok, msg)`` plus its
    side effects. ``fail`` is _run_integrate's refusal closure (files a fail
    envelope + releases the lock); ``release`` releases the merge-queue lock.
    A landed integrate additionally reconciles an ad-hoc thread's
    background-wedge (2026-07-07 #5558/#5564; see juggle_topic_lifecycle
    .reconcile_adhoc_integrate, which self-guards on graph-owned threads)."""
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

    # status == "landed": direct/none — CONFIRM the merge actually stuck
    # BEFORE any side effect (requirement #2, 2026-07-19 KF/KH/KG clobber
    # incident): submit() reporting "landed" only means its own ff-merge
    # returncode was 0 — it is not proof the branch's commits are still an
    # ancestor of main by the time we get here. Detect, refuse, preserve: a
    # merge-miss becomes a safe no-op (branch + worktree kept) instead of
    # `remove_workspace` silently deleting the only remaining copy of the work.
    local_main = rebase_onto.split("/")[-1]
    if not backend.is_ancestor(main_repo_path, worktree_branch, local_main):
        from juggle_integrate_envelope import STEP_MERGE_NOT_CONFIRMED
        return fail(
            STEP_MERGE_NOT_CONFIRMED,
            f"integrate reported {worktree_branch} landed, but its commits are "
            f"NOT confirmed as an ancestor of {local_main} — refusing to delete "
            f"the branch/worktree. Branch preserved at {worktree_path} for "
            f"investigation; re-run `juggle integrate` once resolved.",
        )

    # Count THIS branch's own new commits for the landed notification below —
    # BEFORE teardown removes the worktree/branch out from under us.
    n_commits = _commit_count(worktree_path, rebase_target or rebase_onto, worktree_branch)

    # Recorded AFTER the push (defect C, 2026-07-01): _record_merged_sha
    # checks ancestry against canonical origin/<main>, so recording BEFORE
    # the push tested against an origin/<main> that did not yet contain the
    # commit → merged_sha left NULL and the topic wedged at 'integrating'.
    # Still BEFORE the worktree fields are cleared below (thread → topic
    # binding still resolves).
    _record_merged_sha(db, thread_uuid, main_repo_path, result.landed_rev)

    # An ad-hoc thread's conversation node isn't reconciled by anything else
    # (2026-07-07 #5558/#5564) — do it here, BEFORE the worktree fields are
    # cleared below, same as the merged_sha recording above. No-op for a
    # graph-owned thread (db_graph task or db_topics topic bound).
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

    # Never silent (requirement #3, 2026-07-19): every landed merge is
    # notified, regardless of push_mode — the KF/KH/KG incident's other half
    # was that a clobbered merge produced NO signal at all.
    from dbops import event_kinds as _ek

    push_note = "" if push_mode == "direct" else f" — not pushed (push_mode={push_mode})"
    db.emit_event(
        thread_id=thread_uuid,
        message=(f"⇄ integrate: {worktree_branch} ({n_commits} commit"
                 f"{'s' if n_commits != 1 else ''}) merged to {local_main} "
                 f"@ {(result.landed_rev or '')[:8]}{push_note}"),
        session_id="",
        kind=_ek.INTEGRATE_LANDED,
    )
    return True, f"Integrated {worktree_branch} → {local_main} (push_mode={push_mode})"
