"""dbops.db_mirror — mirror-topic projection for graph-mirrors-threads (Option 1).

A mirror topic is a read-only TRACKER (is_mirror=1 in graph_topics) that
projects a thread's existence and lifecycle into its project's graph. One
mirror per thread, 1:1 by thread_id. Mirror topics are NEVER dispatched by
the watchdog tick and NEVER count toward project progress tallies.

Owns: mirror CRUD (upsert/delete), backfill (all projects), per-project reconcile.
Must not own: topic state-machine transitions (db_topics), dispatching, CLI parsing.
"""

from __future__ import annotations

import logging

from dbops.schema import _now

_log = logging.getLogger(__name__)

_INBOX = "INBOX"

# Thread status → mirror topic state (direct SQL write, bypasses state machine).
_THREAD_TO_STATE: dict[str, str] = {
    "active": "running",
    "idle": "pending",
    "done": "verified",
}


def _mirror_state(thread_status: str) -> str:
    return _THREAD_TO_STATE.get(thread_status or "", "pending")


def mirror_upsert_thread(db, thread_id: str, project_id: str) -> str:
    """Create or update the mirror topic for ``thread_id`` in ``project_id``.

    Single-writer: if a mirror for this thread already exists in a different
    project (project re-assignment), the old mirror is deleted first — so there
    is ALWAYS exactly one mirror per thread. Returns the mirror topic id.
    """
    thread = db.get_thread(thread_id) or {}
    thread_status = thread.get("status") or "active"
    thread_title = (thread.get("title") or thread.get("topic") or thread_id[:8]).strip()
    state = _mirror_state(thread_status)
    mirror_id = f"~{thread_id}"
    now = _now()

    with db._connect() as conn:
        # Check for an existing mirror (may be in a different project on reassign)
        existing = conn.execute(
            "SELECT id, project_id FROM graph_topics WHERE thread_id=? AND is_mirror=1",
            (thread_id,)
        ).fetchone()

        if existing:
            if existing["project_id"] != project_id:
                # Project reassignment: remove the old mirror before inserting new
                conn.execute(
                    "DELETE FROM graph_topics WHERE id=? AND is_mirror=1",
                    (existing["id"],)
                )
                conn.execute(
                    "INSERT INTO graph_topics "
                    "(id, project_id, title, objective, state, thread_id, "
                    "is_mirror, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                    (mirror_id, project_id, thread_title, "", state,
                     thread_id, now, now),
                )
            else:
                # Same project: update title + state
                conn.execute(
                    "UPDATE graph_topics SET title=?, state=?, updated_at=? "
                    "WHERE id=? AND is_mirror=1",
                    (thread_title, state, now, existing["id"])
                )
                mirror_id = existing["id"]
        else:
            conn.execute(
                "INSERT OR IGNORE INTO graph_topics "
                "(id, project_id, title, objective, state, thread_id, "
                "is_mirror, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                (mirror_id, project_id, thread_title, "", state,
                 thread_id, now, now),
            )
        conn.commit()

    return mirror_id


def mirror_delete_thread(db, thread_id: str) -> None:
    """Remove the mirror topic (if any) for ``thread_id``."""
    with db._connect() as conn:
        conn.execute(
            "DELETE FROM graph_topics WHERE thread_id=? AND is_mirror=1",
            (thread_id,)
        )
        conn.commit()


def backfill_mirror_topics(db) -> int:
    """Idempotent backfill: create/update mirror topics for all non-archived
    threads that are assigned to a real project (not INBOX).

    G2-safe: must only be called from the orchestrator (doctor, project create).
    Returns the count of threads processed.
    """
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT id, project_id FROM threads "
                "WHERE project_id IS NOT NULL AND project_id != ? "
                "AND status != 'archived'",
                (_INBOX,)
            ).fetchall()
    except Exception as exc:
        _log.warning("backfill_mirror_topics: query failed: %s", exc)
        return 0

    count = 0
    for row in rows:
        try:
            mirror_upsert_thread(db, row["id"], row["project_id"])
            count += 1
        except Exception as exc:
            _log.warning(
                "backfill_mirror_topics: upsert failed for thread %s: %s",
                row["id"][:8], exc
            )
    _log.info("backfill_mirror_topics: processed %d thread(s)", count)
    return count


def reconcile(db, project_id: str) -> dict:
    """Sync mirror topics for one project.

    - Upsert mirrors for all non-archived assigned threads.
    - Delete mirrors whose threads have since been archived or reassigned.

    Returns dict with 'upserted' and 'deleted' counts.
    """
    upserted = deleted = 0
    try:
        with db._connect() as conn:
            thread_rows = conn.execute(
                "SELECT id FROM threads "
                "WHERE project_id=? AND status != 'archived'",
                (project_id,)
            ).fetchall()
        active_thread_ids = {r["id"] for r in thread_rows}

        with db._connect() as conn:
            mirror_rows = conn.execute(
                "SELECT id, thread_id FROM graph_topics "
                "WHERE project_id=? AND is_mirror=1",
                (project_id,)
            ).fetchall()
        mirrored_thread_ids = {r["thread_id"] for r in mirror_rows}

        # Upsert missing mirrors
        for tid in active_thread_ids:
            try:
                mirror_upsert_thread(db, tid, project_id)
                upserted += 1
            except Exception as exc:
                _log.warning("reconcile: upsert failed for %s: %s", tid[:8], exc)

        # Delete orphan mirrors (thread archived or reassigned)
        orphan_thread_ids = mirrored_thread_ids - active_thread_ids
        for tid in orphan_thread_ids:
            try:
                mirror_delete_thread(db, tid)
                deleted += 1
            except Exception as exc:
                _log.warning("reconcile: delete failed for %s: %s", tid[:8], exc)

    except Exception as exc:
        _log.warning("reconcile: failed for project %s: %s", project_id, exc)

    return {"upserted": upserted, "deleted": deleted}
