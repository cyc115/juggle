"""Migration 61 (async-land stuck-integrating self-heal) — additive
``nodes.pending_merged_sha`` column.

INCIDENT (2026-07-02, async-land): a topic's landed commit was resolved and
proven to exist by ``_record_merged_sha`` but the ancestry check against
``origin/<main>`` (fetched right after the push) failed, so ``merged_sha``
was left NULL. By the time reconcile's self-heal ran, ``_run_integrate`` had
already cleared the thread's ``worktree_branch``/``main_repo_path`` (finalize
step) AND deleted the git branch itself (``remove_workspace``) — leaving no
way to re-derive the merged commit from git. The topic wedged at
'integrating' forever; no orphan sweep could repair it.

``pending_merged_sha`` is a durable, best-effort breadcrumb: the SHA
``_record_merged_sha`` resolved but could not (yet) prove was an ancestor of
main. ``pending_merged_repo`` accompanies it — the repo it was resolved in —
because ``_run_integrate``'s success path ALSO clears the thread's
``main_repo_path`` (finalize step), and ``graph_guards._resolve_topic_repo``'s
self-repo fallback only covers juggle's own repo, not an arbitrary external
one. Reconcile's self-heal re-checks the pending SHA's ancestry against
CURRENT canonical main in the pending repo and promotes it to ``merged_sha``
once proven, independent of the (possibly already deleted) worktree branch.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 60).
Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_61_pending_merged_sha(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        if "pending_merged_sha" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN pending_merged_sha TEXT")
        if "pending_merged_repo" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN pending_merged_repo TEXT")
        conn.commit()
        _log.info("Migration 61: nodes.pending_merged_sha/pending_merged_repo columns ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 61 (pending_merged_sha) skipped: %s", e)
