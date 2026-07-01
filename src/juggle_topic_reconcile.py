"""juggle_topic_reconcile — derive conversation-topic state from child task
states and sync it (2026-06-30 topic-graph-state-unify F3/F6).

Mirrors juggle_graph_reconcile.reconcile_orphaned_inflight: a per-topic-guarded
sweep that NEVER raises. Topic state is DERIVED (juggle_topic_derive) and written
through the canonical set_thread_status → conv_node_mirror path — never a raw
nodes.state UPDATE. Run event-driven (child verified / new human message, F4),
swept every 30s tick (F5), and backfilled once on doctor migrate (F6).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from juggle_topic_derive import derive_topic_state

_log = logging.getLogger("juggle-topic-reconcile")

# Conversation-topic states that are candidates for a derived re-sync.
_CANDIDATE_STATES = ("open", "running", "done")


def close_idle_min() -> int:
    """Minutes of human-message idleness required before a merged topic closes.
    Reads JUGGLE_TOPIC_CLOSE_IDLE_MIN (default 30)."""
    try:
        return int(os.environ.get("JUGGLE_TOPIC_CLOSE_IDLE_MIN", "30"))
    except (TypeError, ValueError):
        return 30


def _child_states(db, topic_id: str) -> list[str]:
    with db._connect() as c:
        rows = c.execute(
            "SELECT state FROM nodes WHERE kind='task' AND parent_id=?", (topic_id,)
        ).fetchall()
    return [r["state"] for r in rows]


def _minutes_since_human_msg(db, topic_id: str, now: datetime) -> float | None:
    """Minutes since the last non-junk human message, or None if there is none."""
    try:
        last_at = db.get_last_exchange(topic_id).get("last_user_at")
    except Exception:
        return None
    if not last_at:
        return None
    try:
        ts = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 60.0


def reconcile_conversation_topics(
    db, *, now: datetime | None = None, close_idle_min: int | None = None,
    only_topic_id: str | None = None,
) -> list[tuple[str, str, str]]:
    """Derive + sync every candidate conversation topic (or just ``only_topic_id``).

    Returns (topic_id, before_state, after_state) for each topic whose state
    changed. Never raises — each topic is guarded. Skips the derived CLOSE when a
    busy agent is bound to the topic thread (G4a live-agent guard).
    """
    now = now or datetime.now(timezone.utc)
    idle = close_idle_min if close_idle_min is not None else globals()["close_idle_min"]()
    try:
        with db._connect() as c:
            if only_topic_id is not None:
                rows = c.execute(
                    "SELECT id, state FROM nodes WHERE kind='conversation' AND id=?",
                    (only_topic_id,),
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in _CANDIDATE_STATES)
                rows = c.execute(
                    f"SELECT id, state FROM nodes WHERE kind='conversation' "
                    f"AND state IN ({placeholders})",
                    _CANDIDATE_STATES,
                ).fetchall()
    except Exception:
        _log.exception("topic reconcile: candidate scan failed — skipping")
        return []

    changed: list[tuple[str, str, str]] = []
    for row in rows:
        topic_id = row["id"]
        before = row["state"]
        try:
            derived = derive_topic_state(
                _child_states(db, topic_id),
                minutes_since_human_msg=_minutes_since_human_msg(db, topic_id, now),
                close_idle_min=idle,
            )
            if derived is None:
                continue
            # No-op if the derived state already matches the stored state.
            if (derived == "done" and before == "done") or (
                derived == "open" and before == "open"
            ):
                continue
            if derived == "done":
                # G4a: never close out from under a busy bound agent.
                if db.get_agent_by_thread(topic_id) is not None:
                    continue
                db.set_thread_status(topic_id, "closed")
            else:  # derived == "open" — reopen / keep active
                db.set_thread_status(topic_id, "active")
            after = db.get_thread(topic_id)["state"]
            if after != before:
                changed.append((topic_id, before, after))
        except Exception:
            _log.exception("topic reconcile: failed for %s — continuing", topic_id)
            continue
    return changed
