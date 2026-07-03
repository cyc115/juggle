"""juggle_land_poller — watchdog land-poller sweep (SPEC §5.3).

Owns: the graph_tick sweep that re-checks 'integrated-unlanded' topics against
their VCS backend's ``land_status`` and promotes/times-out accordingly:
  * landed  -> record merged_sha, transition 'land_confirmed' (the UNCHANGED
    topic_is_merged gate re-verifies ancestry — see dbops.graph_guards),
    then deferred workspace teardown.
  * pending -> leave; retried next tick.
  * past JUGGLE_LAND_TIMEOUT_H -> 'land_fail' + a HIGH action item.

Must not own: the verified gate (dbops.graph_guards.topic_is_merged, reused
unchanged), the node state machine (dbops.db_node_machine), dispatch/claiming
(juggle_graph_dispatch).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from dbops import db_topics
from dbops.db_topics import UnmergedVerifyRefused
from vcs import backend_for

_log = logging.getLogger("juggle-land-poller")

DEFAULT_LAND_TIMEOUT_H = 72.0


def _land_timeout_h() -> float:
    raw = os.environ.get("JUGGLE_LAND_TIMEOUT_H")
    if not raw:
        return DEFAULT_LAND_TIMEOUT_H
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LAND_TIMEOUT_H


def _past_timeout(topic: dict) -> bool:
    updated = topic.get("updated_at")
    if not updated:
        return False
    try:
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_land_timeout_h())
    return ts < cutoff


def _resolve_repo_and_ticket(db, topic: dict) -> tuple[str, str]:
    """repo (thread.main_repo_path) + ticket (nodes.submitted_rev — SPEC §7.2,
    opaque). Either missing means the topic can't be polled this tick."""
    thread_id = topic.get("thread_id")
    thread = db.get_thread(thread_id) if thread_id else None
    repo = ((thread or {}).get("main_repo_path") or "").strip()
    ticket = (topic.get("submitted_rev") or "").strip()
    return repo, ticket


def _teardown_topic_workspace(db, topic: dict, repo: str) -> None:
    """Deferred workspace teardown (SPEC §5.3): the worktree/branch were kept
    alive through 'integrated-unlanded' so the poller could still resolve
    land_status; remove them now that the topic is confirmed verified."""
    thread_id = topic.get("thread_id")
    thread = db.get_thread(thread_id) if thread_id else None
    worktree_path = (thread or {}).get("worktree_path") or ""
    if not worktree_path:
        return
    try:
        backend_for(repo).remove_workspace(repo, worktree_path)
    except Exception:
        _log.exception(
            "land poller: workspace teardown failed for topic %s (%s)",
            topic.get("id"), worktree_path,
        )
    db.update_thread(thread_id, worktree_path="", worktree_branch="", main_repo_path="")


def poll_unlanded_topics(db, project_id: str) -> dict:
    """Sweep 'integrated-unlanded' topics for one project. Never raises — one
    bad topic must not stop the sweep (matches the orphan-reconcile precedent
    in juggle_graph_dispatch.graph_tick)."""
    stats = {"landed": [], "pending": [], "timed_out": [], "errors": []}
    topics = [t for t in db_topics.list_topics(db, project_id)
              if t["state"] == "integrated-unlanded"]
    for topic in topics:
        tid = topic["id"]
        try:
            repo, ticket = _resolve_repo_and_ticket(db, topic)
            if not repo or not ticket:
                _log.warning(
                    "land poller: topic %s missing repo/ticket — skipping", tid
                )
                stats["errors"].append(tid)
                continue
            status = backend_for(repo).land_status(repo, ticket)
            if status.state == "landed":
                db_topics.set_topic_merged_sha(db, tid, status.landed_rev)
                try:
                    db_topics.topic_transition(db, tid, "land_confirmed")
                except UnmergedVerifyRefused:
                    # Gate refused (not-yet-ancestor merged_sha, e.g. remote not
                    # yet fetched) — topic_transition RAISES rather than
                    # returning a non-'verified' state, so this must be caught
                    # explicitly. Stays 'integrated-unlanded', retried next tick.
                    stats["pending"].append(tid)
                    continue
                _teardown_topic_workspace(db, topic, repo)
                stats["landed"].append(tid)
                continue
            if status.state == "failed":
                db_topics.topic_transition(db, tid, "land_fail")
                db.add_action_item(
                    thread_id=None,
                    message=(f"⚠️ Topic {tid} land failed (ticket {ticket}): "
                             f"{status.detail}"),
                    type_="manual_step", priority="high",
                )
                stats["timed_out"].append(tid)
                continue
            # state == "unlanded" — still in flight.
            if _past_timeout(topic):
                db_topics.topic_transition(db, tid, "land_fail")
                db.add_action_item(
                    thread_id=None,
                    message=(f"⚠️ Topic {tid} land timed out after "
                             f"{_land_timeout_h():g}h (ticket {ticket}). "
                             f"Investigate the land queue manually."),
                    type_="manual_step", priority="high",
                )
                stats["timed_out"].append(tid)
            else:
                stats["pending"].append(tid)
        except Exception:
            _log.exception("land poller: unexpected error on topic %s", tid)
            stats["errors"].append(tid)
    return stats
