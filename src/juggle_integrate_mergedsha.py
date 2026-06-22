"""juggle_integrate_mergedsha — record the merged commit SHA on a topic.

Extracted from juggle_cmd_integrate (≤loc_gate budget) when the concurrent-
integrate pileup hardening (2026-06-21) pushed that module past budget. Pure
helper: best-effort provenance for the T-verified-merged-sha gate, never blocks
integrate. Re-exported from juggle_cmd_integrate so the existing test
import/patch surface (juggle_cmd_integrate._record_merged_sha) keeps working.
"""

import subprocess

from juggle_repo_binding import canonical_main_ref as _canonical_main_ref


def _record_merged_sha(db, thread_uuid: str, repo: str, ref: str) -> None:
    """Record the merged commit (``ref`` tip, now on main) on the topic bound to
    this thread (T-verified-merged-sha). The single source of truth for the
    verified gate. Fail-soft: best-effort provenance, never blocks integrate.

    Guards (2026-06-16 phantom-SHA fix):
      1. Object must exist: ``git cat-file -e <sha>``.
      2. SHA must be an ancestor of the canonical main (``origin/<main>`` after
         fetch; fallback to local main). A phantom or unmerged SHA is silently
         skipped — merged_sha is left NULL so the verified-gate stays closed.
    """
    try:
        from dbops import db_topics
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
        if not topic:
            return

        sha_result = subprocess.run(
            ["git", "-C", repo, "rev-parse", ref],
            capture_output=True, text=True,
        )
        if sha_result.returncode != 0 or not sha_result.stdout.strip():
            return
        sha = sha_result.stdout.strip()

        # Guard 1: object must exist in the repo's object store.
        cat_file = subprocess.run(
            ["git", "-C", repo, "cat-file", "-e", sha],
            capture_output=True, text=True,
        )
        if cat_file.returncode != 0:
            import logging
            logging.getLogger(__name__).warning(
                "_record_merged_sha: object %s does not exist in %s — skipping",
                sha, repo,
            )
            return

        # Guard 2: SHA must be an ancestor of canonical main.
        canonical = _canonical_main_ref(repo)
        if canonical is None:
            import logging
            logging.getLogger(__name__).warning(
                "_record_merged_sha: cannot resolve canonical main in %s — skipping",
                repo,
            )
            return
        ancestor_check = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", sha, canonical],
            capture_output=True, text=True,
        )
        if ancestor_check.returncode != 0:
            import logging
            logging.getLogger(__name__).warning(
                "_record_merged_sha: %s is NOT an ancestor of %s in %s — skipping",
                sha, canonical, repo,
            )
            return

        db_topics.set_topic_merged_sha(db, topic["id"], sha)
    except Exception:
        pass
