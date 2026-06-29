"""juggle_graph_dispatch_topics — TOPIC-level claim / sweep / give-up helpers.

Extracted from juggle_graph_dispatch (2026-06-11, R9 LOC gate): the topic twins
of the task claim/sweep/give-up trio. Same SQL pattern, graph_topics table.
juggle_graph_dispatch re-exports these (bottom import) so callers/tests keep
using `juggle_graph_dispatch.claim_topic` etc., and `graph_tick` (which stays
there) drives them.

Owns: the atomic ready→dispatching TOPIC claim (a sanctioned graph_topics.state
writer besides db_topics.topic_transition — a compare-and-swap cannot go
through read-then-write), the stale TOPIC-claim sweep, and the retry-cap
give-up (failed-exec + derived-dependent blocking + one final action item).
Must not own: the tick loop / hydration / dispatch path (juggle_graph_dispatch),
topic state semantics (dbops.db_topics).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from dbops import db_topics
from dbops.schema import _now
from juggle_graph_dispatch import MAX_DISPATCH_FAILS, STALE_CLAIM_SECS

_log = logging.getLogger("juggle-graph-dispatch")


def claim_topic(db, topic_id: str) -> bool:
    """Atomic ready→dispatching TOPIC claim (DA B4 pattern). True iff won.

    P8 (Task 4.2): the CAS writes ``nodes`` (authoritative) in lockstep with the
    legacy graph_topics row. A non-topic id (e.g. a conversation node) has no
    'ready' topic row, so the claim simply loses (won=0)."""
    from dbops.state_write import cas_state

    with db._connect() as conn:
        won = cas_state(conn, topic_id, frm="ready", to="dispatching", now=_now())
        conn.commit()
        return won == 1


def sweep_stale_topic_claims(db, project_id: str) -> list[str]:
    """dispatching >10 min with no thread → ready (crash-safe, idempotent)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_SECS)
    ).isoformat()
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE kind='topic' "
            "AND project_id=? AND state='dispatching' AND updated_at < ? "
            "AND id NOT IN (SELECT node_id FROM node_edges WHERE kind='dispatch')",
            (project_id, cutoff),
        ).fetchall()
    stale = [r["id"] for r in rows]
    for tid in stale:
        db_topics.topic_transition(db, tid, "stale_reset")
        _log.warning("graph tick: stale TOPIC claim swept, %s → ready", tid)
    return stale


def _give_up_topic_dispatch(db, topic_id: str, err: Exception) -> None:
    """Retry cap reached: topic → failed-exec + derived-dependent blocking +
    ONE final action item (mirror of _give_up_dispatch)."""
    db_topics.mark_topic_exec_failed(db, topic_id)
    blocked = db_topics.propagate_topic_failure(db, topic_id)
    detail = f" Dependent topics blocked: {', '.join(blocked)}." if blocked else ""
    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ Autopilot gave up on topic {topic_id} after "
            f"{MAX_DISPATCH_FAILS} consecutive dispatch failures: {err}.{detail} "
            f"Fix the dispatch path, then reload the graph spec to resume."
        ),
        type_="failure",
        priority="high",
    )
    _log.error("graph tick: topic %s failed-exec after %d dispatch failures",
               topic_id, MAX_DISPATCH_FAILS)
