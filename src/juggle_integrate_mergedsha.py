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
    former standalone ``cat-file -e`` existence check. A phantom or unmerged
    SHA is silently skipped — merged_sha is left NULL so the gate stays closed.
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
            return
        if not backend.is_ancestor(repo, sha, canonical):
            import logging
            logging.getLogger(__name__).warning(
                "_record_merged_sha: %s is NOT an ancestor of %s in %s — skipping",
                sha, canonical, repo,
            )
            return

        db_topics.set_topic_merged_sha(db, topic["id"], sha)
    except Exception:
        pass
