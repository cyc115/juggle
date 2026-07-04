"""juggle_integrate_mergedsha — record the merged commit SHA on a topic.

Extracted from juggle_cmd_integrate (≤loc_gate budget) when the concurrent-
integrate pileup hardening (2026-06-21) pushed that module past budget. Pure
helper: best-effort provenance for the T-verified-merged-sha gate, never blocks
integrate. Re-exported from juggle_cmd_integrate so the existing test
import/patch surface (juggle_cmd_integrate._record_merged_sha) keeps working.
"""

from juggle_repo_binding import canonical_main_ref as _canonical_main_ref
from vcs import backend_for


def _record_merged_sha(db, thread_uuid: str, repo: str, ref: str) -> None:
    """Record the merged commit (``ref`` tip, now on main) on the topic bound to
    this thread (T-verified-merged-sha). The single source of truth for the
    verified gate. Fail-soft: best-effort provenance, never blocks integrate.

    Guard (2026-06-16 phantom-SHA fix): SHA must be an ancestor of the
    canonical main (``origin/<main>`` after fetch; fallback to local main) —
    ``is_ancestor`` is fail-closed on a nonexistent object, subsuming the
    former standalone ``cat-file -e`` existence check. A phantom SHA is
    silently skipped outright — merged_sha is left NULL so the gate stays
    closed. A SHA that resolved to a real object but failed the ancestry check
    (2026-07-02 async-land incident: the check can transiently miss a just-
    pushed commit) is stashed on ``pending_merged_sha`` instead of dropped —
    worktree teardown (right after this call, in ``_run_integrate``) deletes
    the git branch and clears the thread's worktree fields, so this is the
    only surviving breadcrumb reconcile's self-heal can re-check later
    (``dbops.db_topics_reconcile._heal_merged_sha``).
    """
    try:
        from dbops import db_topics
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
        if not topic:
            return

        backend = backend_for(repo)
        sha = backend.resolve(repo, ref)
        if not sha:
            return

        canonical = _canonical_main_ref(repo)
        if canonical is None:
            import logging
            logging.getLogger(__name__).warning(
                "_record_merged_sha: cannot resolve canonical main in %s — skipping",
                repo,
            )
            db_topics.set_topic_pending_merged_sha(db, topic["id"], sha, repo=repo)
            return
        if not backend.is_ancestor(repo, sha, canonical):
            import logging
            logging.getLogger(__name__).warning(
                "_record_merged_sha: %s is NOT an ancestor of %s in %s — skipping",
                sha, canonical, repo,
            )
            db_topics.set_topic_pending_merged_sha(db, topic["id"], sha, repo=repo)
            return

        db_topics.set_topic_merged_sha(db, topic["id"], sha)
        db_topics.set_topic_pending_merged_sha(db, topic["id"], None)
        # merged_sha has definitively landed (ancestry proven above) — auto-ack
        # any stale completed-but-UNMERGED escalation the watchdog filed while
        # this topic sat unmerged. The integrate happy path never passes through
        # orphan reconcile, so without this the blocker lingers (2026-07-04
        # autoclear fix; incident: T-gp-cancel merged yet item 5308 stayed open).
        from dbops.orphan_autoclear import clear_stale_unmerged_escalations
        clear_stale_unmerged_escalations(db, topic["id"])
    except Exception:
        pass
