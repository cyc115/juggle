"""dbops.orphan_guard — detect & surface completed-but-unmerged topics (G5).

Incident (2026-06-17): a false-negative in ``JuggleTmuxManager.send_task`` made
the watchdog treat a successful dispatch as failed, so the topic was never
tracked for integrate. When the coder's ``complete-agent`` closed the topic, its
work sat committed-in-worktree but UNMERGED, and ``juggle integrate`` reported
"Missing worktree fields — nothing to integrate". G1 (graph_guards.topic_is_merged)
already keeps such a topic out of ``verified``; this guard *detects* the stranded
topics and files a HIGH action item so a completed topic is NEVER silently closed
without merge — it always surfaces a blocker.

Pure detection + flagging. Owns no state transition (db_topics stays the writer)
and no integrate logic (juggle_cmd_integrate).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbops.graph_guards import topic_is_merged

# watchdog_events.event_type used to dedup repeated flags for the same topic.
_ORPHAN_EVENT = "topic_unmerged_orphan"
# Sentinel agent_id (watchdog_events.agent_id is NOT NULL) — this guard is not
# bound to a single agent.
_GUARD_AGENT_ID = "orphan-guard"


def find_unmerged_completed_topics(db) -> list[dict]:
    """Return non-mirror, non-verified topics whose member tasks are ALL verified
    but whose work is NOT merged to main (``topic_is_merged`` is False).

    These are the completed-but-unmerged orphans the close-before-integrate path
    strands. A topic with zero tasks, any unfinished task, or proven-merged work
    is excluded.
    """
    with db._connect() as conn:
        topic_rows = conn.execute(
            "SELECT * FROM graph_topics "
            "WHERE COALESCE(is_mirror, 0) = 0 AND state != 'verified'"
        ).fetchall()
        topics = [dict(r) for r in topic_rows]

    orphans: list[dict] = []
    for topic in topics:
        with db._connect() as conn:
            task_states = [
                r[0]
                for r in conn.execute(
                    "SELECT state FROM graph_tasks WHERE topic_id=?", (topic["id"],)
                ).fetchall()
            ]
        if not task_states:
            continue
        if not all(s == "verified" for s in task_states):
            continue
        if topic_is_merged(db, topic["id"]):
            continue
        orphans.append(topic)
    return orphans


def flag_unmerged_completed_topics(db, *, dedup_window_hours: float = 24.0) -> list[str]:
    """Detect completed-but-unmerged topics and file a HIGH action item for each.

    Deduped via ``watchdog_events`` (one flag per topic per ``dedup_window_hours``)
    so the watchdog tick can call this every cycle. Returns the topic ids flagged
    this pass.
    """
    orphans = find_unmerged_completed_topics(db)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=dedup_window_hours)).isoformat()

    flagged: list[str] = []
    for topic in orphans:
        topic_id = topic["id"]
        with db._connect() as conn:
            recent = conn.execute(
                "SELECT id FROM watchdog_events "
                "WHERE thread_id=? AND event_type=? AND created_at > ?",
                (topic_id, _ORPHAN_EVENT, cutoff),
            ).fetchone()
        if recent:
            continue
        label = topic.get("title") or topic_id
        db.add_action_item(
            thread_id=topic.get("thread_id"),
            message=(
                f"⚠️ topic {label} [{topic_id}] completed but UNMERGED "
                f"(all tasks verified, no merged_sha) — run `juggle integrate "
                f"{topic_id}` or recover its worktree. Never close-without-merge."
            ),
            type_="manual_step",
            priority="high",
        )
        db.add_watchdog_event(_GUARD_AGENT_ID, topic_id, _ORPHAN_EVENT)
        flagged.append(topic_id)
    return flagged
