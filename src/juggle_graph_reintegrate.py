"""juggle_graph_reintegrate — the watchdog re-integrate driver (integrate-wedge fix 1).

Incident (2026-07-03 integrate-wedge RCA): the only merge-lander
(complete-agent → ``_run_integrate``) runs ONCE, inline. A single miss wedged a
topic in ``state='integrating'`` FOREVER — graph_tick re-dispatches only 'ready'
topics, the repair sweep needs a ``fail_envelope`` (none was written), and
orphan-reconcile skips a topic bound to a busy agent. Three topics sat wedged
1–1.5 h with real unmerged work and zero visible action.

This module is the missing durable reconcile-repair path: a LEVEL-TRIGGERED
sweep (the Kubernetes controller / systemd ``Restart=on-failure`` shape — see
research/2026-07-03-watchdog-reconciliation-patterns.md) run every watchdog tick.
Observed git state is the oracle:

* LANDED (ff/true-merge OR rebased/cherry-picked, via the two-tier
  ``graph_guards.resolve_landed_sha`` oracle) → heal ``merged_sha`` + advance to
  'verified' via reconcile. NEVER re-merge (re-merging rebased work duplicates
  commits / raises a spurious conflict — the amendment's blind spot).
* non-landed + real commits ahead + NO live bound agent + backoff elapsed →
  idempotently re-run ``_run_integrate``; on a real failure the topic goes
  'failed-integration' (its ``fail_envelope`` routes it to the existing repair
  sweep).
* Forget (k8s ``workqueue.Forget``): once a topic leaves 'integrating' (→
  verified on landing, → failed-integration on failure) its per-topic backoff
  state is dropped so the driver never hot-loops on it.

Must not own: the topic state machine (dbops.db_topics), the merge mechanics
(juggle_cmd_integrate._run_integrate), or the dispatch claim loop
(juggle_graph_dispatch) — it only re-drives and reconciles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

# Imported at module scope so tests can patch juggle_graph_reintegrate._run_integrate.
from juggle_cmd_integrate import _run_integrate

_log = logging.getLogger("juggle-graph-reintegrate")

# A topic must have sat in 'integrating' at least this long before the driver
# re-drives it — long enough that the owning agent's own inline integrate
# (complete-agent → _run_integrate) is not still in flight (2026-07-02 grace
# convention). The incident wedge was 1–1.5 h, far past this.
REINTEGRATE_GRACE_SECS = 300
# Minimum spacing between re-drive attempts for the SAME topic — integrate runs
# the full suite, so never hammer it every ~15 s tick (k8s AddRateLimited analog).
REINTEGRATE_BACKOFF_SECS = 300
# Soft-failure backstop: after this many attempts that produced no fail_envelope
# (pre-flight refusals — lock timeout, mis-bind), escalate to failed-integration
# so the topic never re-drives forever.
MAX_REINTEGRATE_ATTEMPTS = 3

# Per-topic backoff state, keyed by (db_path, topic_id). Reset via reset_backoff.
_backoff: dict[tuple[str, str], dict] = {}


def reset_backoff() -> None:
    """Drop all per-topic backoff state (test hook / config reload)."""
    _backoff.clear()


def _key(db, topic_id: str) -> tuple[str, str]:
    return (str(getattr(db, "db_path", "")), topic_id)


def _forget(db, topic_id: str) -> None:
    """k8s workqueue.Forget: stop tracking a topic that left 'integrating'."""
    _backoff.pop(_key(db, topic_id), None)


def _secs_since(iso_ts: str | None, now: datetime) -> float:
    if not iso_ts:
        return float("inf")  # unknown age → treat as long-elapsed (never blocks)
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds()


def _grace_elapsed(topic: dict, now: datetime) -> bool:
    return _secs_since(topic.get("updated_at"), now) >= REINTEGRATE_GRACE_SECS


def _backoff_elapsed(db, topic_id: str, now: datetime) -> bool:
    st = _backoff.get(_key(db, topic_id))
    if not st:
        return True
    last = st.get("last_attempt")
    return last is None or (now - last).total_seconds() >= REINTEGRATE_BACKOFF_SECS


def _record_attempt(db, topic_id: str, now: datetime) -> int:
    st = _backoff.setdefault(_key(db, topic_id), {"attempts": 0, "last_attempt": None})
    st["attempts"] += 1
    st["last_attempt"] = now
    return st["attempts"]


def _has_live_bound_agent(db, thread_id: str | None) -> bool:
    if not thread_id:
        return False
    try:
        return db.get_agent_by_thread(thread_id) is not None
    except Exception:
        return False


def _real_commits_ahead(thread: dict) -> bool:
    """True iff the topic's worktree exists and has committed work ahead of
    trunk — genuine unmerged work to integrate (not an empty branch / lost
    worktree, which the orphan guard surfaces instead)."""
    from pathlib import Path
    from vcs import backend_for

    wp = (thread.get("worktree_path") or "").strip()
    mrp = (thread.get("main_repo_path") or "").strip()
    if not wp or not mrp or not Path(wp).exists():
        return False
    try:
        backend = backend_for(mrp)
        trunk = backend.trunk(mrp)
        if not trunk:
            return False
        return backend.has_changes(wp, since=trunk)
    except Exception:
        return False


def _reintegrate_topic(db, topic: dict, session_id: str, now: datetime) -> str | None:
    """Reconcile-and-maybe-re-drive one 'integrating' topic. Returns the topic
    id if it was healed/driven/routed this pass, else None. Never raises."""
    from dbops.db_topics import get_topic, reconcile_topic_state
    from juggle_cmd_agents_graph_topics import mark_graph_topic

    tid = topic["id"]

    # 1. Level-triggered heal: git reality is the oracle. reconcile re-derives
    #    the topic from its tasks via the two-tier _heal_merged_sha — a LANDED
    #    (incl. rebased) topic stamps merged_sha and advances to 'verified'.
    try:
        state = reconcile_topic_state(db, tid)
    except Exception:
        _log.exception("reintegrate: reconcile failed for %s", tid)
        return None
    if state == "verified":
        _forget(db, tid)
        return tid
    if state != "integrating":
        _forget(db, tid)  # moved to a failure verdict elsewhere — repair owns it
        return None

    # 2. Still 'integrating' and NOT landed → consider re-driving integrate.
    thread_id = topic.get("thread_id")
    if not thread_id:
        return None  # unbound — worktree gone; orphan guard surfaces it
    if _has_live_bound_agent(db, thread_id):
        return None  # owning agent may still be finalizing — never re-drive under it
    if not _grace_elapsed(topic, now) or not _backoff_elapsed(db, tid, now):
        return None
    try:
        thread = db.get_thread(thread_id) or {}
    except Exception:
        return None
    if not _real_commits_ahead(thread):
        return None  # empty branch / lost worktree — not this driver's job

    attempts = _record_attempt(db, tid, now)
    ok, msg = _run_integrate(thread, db)
    if ok:
        try:
            reconcile_topic_state(db, tid)
        except Exception:
            _log.exception("reintegrate: post-success reconcile failed for %s", tid)
        _forget(db, tid)
        _log.info("reintegrate: re-drove wedged topic %s → landed", tid)
        return tid

    # 3. Failure. Route to 'failed-integration' (→ repair sweep) when integrate
    #    wrote a real fail_envelope, or when the soft-failure backstop is hit.
    #    Otherwise (pre-flight refusal, no envelope) leave it 'integrating' and
    #    back off for a later retry.
    refreshed = get_topic(db, tid) or {}
    if refreshed.get("fail_envelope") or attempts >= MAX_REINTEGRATE_ATTEMPTS:
        try:
            mark_graph_topic(db, thread_id, False, None, session_id)
        except Exception:
            _log.exception("reintegrate: failed to route %s to failed-integration", tid)
            return None
        _forget(db, tid)
        _log.warning("reintegrate: topic %s failed re-integrate → failed-integration: %s",
                     tid, msg)
        return tid
    _log.info("reintegrate: soft re-integrate refusal for %s (attempt %d) — backing off: %s",
              tid, attempts, msg)
    return None


def run_reintegrate_tick(db) -> list[str]:
    """Watchdog-tick entry point (called from juggle_graph_repair.run_tick_sweeps,
    right after graph_tick each cycle): derive the active project set + session and
    re-drive every wedged 'integrating' topic. Kept out of graph_tick to respect
    that module's LOC budget."""
    from juggle_graph_dispatch import _all_project_ids, _session_id

    return sweep_reintegrate(db, _all_project_ids(db), session_id=_session_id(db))


def sweep_reintegrate(db, project_ids, *, session_id: str = "", now=None) -> list[str]:
    """graph_tick entry point: re-drive every wedged 'integrating' topic across
    the given projects. Fail-soft per-project and per-topic — one bad topic never
    wedges the tick. Returns the ids healed / driven / routed this pass."""
    from dbops import db_topics

    now = now or datetime.now(timezone.utc)
    driven: list[str] = []
    for pid in project_ids:
        try:
            topics = db_topics.list_topics(db, pid)
        except Exception:
            _log.exception("reintegrate: topic scan failed for project %s", pid)
            continue
        for topic in topics:
            if topic.get("state") != "integrating":
                continue
            try:
                res = _reintegrate_topic(db, topic, session_id, now)
            except Exception:
                _log.exception("reintegrate: unexpected error on topic %s", topic.get("id"))
                continue
            if res:
                driven.append(res)
    return driven
