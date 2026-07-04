"""dbops.db_reintegrate — durable per-topic re-integrate backoff state.

Persists the watchdog re-integrate driver's attempt counter, last-attempt
timestamp, and last-spawned gate pid on the topic's ``nodes`` row (Migration 71),
so a watchdog restart can no longer wipe them.

Incident (2026-07-03/04 integrate-wedge #2, RC4 of 4): this state used to live in
an in-memory module dict (juggle_graph_reintegrate._backoff). The watchdog
restarts on every plugin-HEAD advance — which every successful integrate causes —
so the dict was cleared constantly: perpetual 'attempt 1', the spawn-failure
attempt bound never accumulated, and the lost proc handle let a restart
double-spawn a gate for the same topic. These helpers move that state into the DB.

Owns: read/record/forget of the three reintegrate_* columns.
Must not own: the re-drive decision logic (juggle_graph_reintegrate) or the topic
state machine (dbops.db_topics).
"""
from __future__ import annotations

from dbops.db_graph import _cx


def get_state(db, topic_id, conn=None) -> tuple[int, str | None, int | None]:
    """Return (attempts, last_attempt_iso, pid) for a topic — (0, None, None) if
    the topic is absent or the columns are unset."""
    with _cx(db, conn) as c:
        row = c.execute(
            "SELECT reintegrate_attempts, reintegrate_last_attempt, reintegrate_pid "
            "FROM nodes WHERE id=? AND kind='topic'", (topic_id,)
        ).fetchone()
    if not row:
        return 0, None, None
    return (row[0] or 0, row[1], row[2])


def get_attempts(db, topic_id, conn=None) -> int:
    return get_state(db, topic_id, conn)[0]


def record_attempt(db, topic_id, now_iso, pid=None, conn=None) -> int:
    """Increment the attempt counter, stamp last_attempt, and record the pid of
    the detached integrate just spawned (single-flight guard). Returns the new
    attempt count. Recorded even on spawn failure (pid=None) so the backoff +
    attempt-bound backstop still apply."""
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE nodes SET "
            "reintegrate_attempts = COALESCE(reintegrate_attempts, 0) + 1, "
            "reintegrate_last_attempt = ?, reintegrate_pid = ? "
            "WHERE id=? AND kind='topic'",
            (now_iso, pid, topic_id),
        )
        row = c.execute(
            "SELECT reintegrate_attempts FROM nodes WHERE id=? AND kind='topic'",
            (topic_id,),
        ).fetchone()
    return (row[0] if row else 0) or 0


def forget(db, topic_id, conn=None) -> None:
    """k8s workqueue.Forget: drop the persisted backoff state once the topic
    leaves 'integrating', so the driver never hot-loops on it."""
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE nodes SET reintegrate_attempts = 0, "
            "reintegrate_last_attempt = NULL, reintegrate_pid = NULL "
            "WHERE id=? AND kind='topic'",
            (topic_id,),
        )
