"""juggle_graph_reintegrate — the watchdog re-integrate driver (integrate-wedge fix 1).

Incident (2026-07-03 integrate-wedge RCA): the only merge-lander (complete-agent
→ ``_run_integrate``) runs ONCE, inline. A single miss wedged a topic in
``state='integrating'`` FOREVER — graph_tick re-dispatches only 'ready' topics,
the repair sweep needs a ``fail_envelope`` (none written), and orphan-reconcile
skips a topic bound to a busy agent. Three topics sat wedged 1–1.5 h.

This module is the missing durable reconcile-repair path: a LEVEL-TRIGGERED
sweep (k8s controller / systemd ``Restart=on-failure`` shape — see
research/2026-07-03-watchdog-reconciliation-patterns.md) run every watchdog tick.
Observed git state is the oracle:

* LANDED (ff/true-merge OR rebased/cherry-picked, via the two-tier
  ``graph_guards.resolve_landed_sha`` oracle) → heal ``merged_sha`` + advance to
  'verified' via reconcile. NEVER re-merge (re-merging rebased work duplicates
  commits / raises a spurious conflict — the amendment's blind spot).
* non-landed + real commits ahead + NO live bound agent + backoff elapsed →
  spawn a DETACHED ``juggle integrate`` subprocess. The gate NEVER runs inline
  on the tick — that was the 2026-07-03 livelock (the ~7-min gate blew the 90s
  tickguard budget → 'hang suspected' daemon restart killed it mid-run → retry
  next boot → 5 restarts in 12min, zero merges). The detached process survives a
  daemon restart (``start_new_session``); the per-repo integrate lock + heartbeat
  serialize it (single-flight). Its outcome reconciles on a LATER tick: landed →
  'verified'; a real failure wrote a ``fail_envelope`` → 'failed-integration'.
* Forget (k8s ``workqueue.Forget``): once a topic leaves 'integrating' its
  per-topic backoff state is dropped so the driver never hot-loops on it.

Must not own: the topic state machine (dbops.db_topics), the merge mechanics
(juggle_cmd_integrate), or the dispatch claim loop — it only re-drives + reconciles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from juggle_graph_reintegrate_liveness import (  # noqa: F401 (re-exported for existing test imports)
    _bound_agent_blocks, _completion_recorded, _has_live_bound_agent,
)

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
# REINTEGRATE_STALE_BOUND_SECS (escape-hatch backstop, integrate-wedge #2 RC2)
# now lives in juggle_graph_reintegrate_liveness with the guard it bounds.

# Per-topic backoff state (attempts + last_attempt + spawned gate pid) is now
# PERSISTED on the topic's node row (dbops.db_reintegrate / Migration 71), not an
# in-memory dict wiped on every watchdog restart (integrate-wedge #2, RC4:
# perpetual 'attempt 1', a never-accumulating attempt bound, and a lost
# single-flight handle that let a restart double-spawn a gate).


def reset_backoff() -> None:
    """No-op: backoff state is durable in the DB (Migration 71), not in memory.
    Retained because tests/config-reload call it — a restart must NOT drop it."""
    return None


def _forget(db, topic_id: str) -> None:
    """k8s workqueue.Forget: drop the persisted backoff state for a topic that
    left 'integrating' (fail-soft via the sweep's per-topic guard)."""
    from dbops import db_reintegrate

    db_reintegrate.forget(db, topic_id)


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
    from dbops import db_reintegrate

    _, last_attempt, _ = db_reintegrate.get_state(db, topic_id)
    return _secs_since(last_attempt, now) >= REINTEGRATE_BACKOFF_SECS


def _record_attempt(db, topic_id: str, now: datetime, proc=None) -> int:
    """Durably bump the attempt counter, stamp last_attempt, and record the
    spawned gate's pid (single-flight guard) on the topic's node row."""
    from dbops import db_reintegrate

    pid = getattr(proc, "pid", None)  # the detached integrate we just spawned
    return db_reintegrate.record_attempt(db, topic_id, now.isoformat(), pid=pid)


def _spawn_alive(db, topic_id: str) -> bool:
    """True iff the gate last spawned for this topic is still running — a
    CROSS-PROCESS check (recorded pid + os.kill liveness probe) that survives a
    watchdog restart, so a restart cannot double-spawn the same topic's gate."""
    from dbops import db_reintegrate
    from juggle_integrate_lock import _pid_alive

    _, _, pid = db_reintegrate.get_state(db, topic_id)
    return _pid_alive(int(pid)) if pid else False


def _real_commits_ahead(thread: dict) -> bool:
    """True iff the topic's worktree exists and has committed work ahead of trunk
    — genuine unmerged work (not an empty branch / lost worktree; orphan-guarded)."""
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


def _spawn_detached_integrate(thread: dict, db):
    """Spawn ``juggle integrate <thread>`` DETACHED; return its handle (None on
    spawn failure). NEVER blocks the tick. Thin wrapper over the shared
    juggle_integrate_spawn.spawn_detached_integrate — the SAME spawn complete-agent
    uses (2026-07-04 inline-gate death by watchdog respawn). Kept as a module
    symbol so the driver's own test patches (single-flight, mid-gate-kill) target
    it. ``start_new_session`` detaches it from the watchdog's process group so a
    tickguard daemon restart can't kill the gate mid-run (the 2026-07-03
    livelock); the per-repo integrate lock + heartbeat serialize it (single-flight)
    even across a restart. ``JUGGLE_ORCHESTRATOR=1`` marks it watchdog-owned so the
    integrate guard permits the call."""
    from juggle_integrate_spawn import spawn_detached_integrate

    return spawn_detached_integrate(thread, db)


def _reintegrate_topic(db, topic: dict, session_id: str, now: datetime) -> str | None:
    """Reconcile one 'integrating' topic and, if wedged, spawn a DETACHED
    integrate (the gate NEVER runs inline). Returns the topic id if it was
    healed / spawned / routed this pass, else None. Never raises."""
    from dbops.db_topics import get_topic, reconcile_topic_state
    from juggle_cmd_agents_graph_topics import mark_graph_topic

    tid = topic["id"]

    # 1. Level-triggered heal (git reality is the oracle): a LANDED (incl.
    #    rebased) topic stamps merged_sha → 'verified'. Runs FIRST so a just-
    #    landed topic wins over a redundant integrate's spurious empty-branch env.
    try:
        state = reconcile_topic_state(db, tid)
    except Exception:
        _log.exception("reintegrate: reconcile failed for %s", tid)
        return None
    if state == "verified":
        _forget(db, tid)
        from juggle_cmd_agents_adhoc_wrapper import propagate_wrapper_outcome_to_task
        propagate_wrapper_outcome_to_task(  # Bug#1-still-broken: mirror onto real task
            db, tid, topic.get("thread_id"), state, topic.get("handoff"), session_id)
        return tid
    if state != "integrating":
        _forget(db, tid)  # moved to a failure verdict elsewhere — repair owns it
        return None

    # 2. Still 'integrating' and NOT landed.
    thread_id = topic.get("thread_id")
    if not thread_id:
        return None  # unbound — worktree gone; orphan guard surfaces it
    if _bound_agent_blocks(db, topic, thread_id, now):
        return None  # owning agent may still be finalizing — never re-drive under it

    # 3. A detached integrate may still be running its gate — never re-spawn /
    #    count / fail it in flight (its outcome reconciles on a later tick).
    if _spawn_alive(db, tid):
        return None

    # 4. A prior detached attempt finished WITHOUT landing → route to
    #    'failed-integration' (repair sweep) on a real fail_envelope or the
    #    soft-failure backstop (never re-spawn forever).
    from dbops import db_reintegrate
    refreshed = get_topic(db, tid) or {}
    attempts = db_reintegrate.get_attempts(db, tid)
    if refreshed.get("fail_envelope") or attempts >= MAX_REINTEGRATE_ATTEMPTS:
        try:
            mark_graph_topic(db, thread_id, False, None, session_id, topic_id=tid)
        except Exception:
            _log.exception("reintegrate: failed to route %s to failed-integration", tid)
            return None
        _forget(db, tid)
        _log.warning("reintegrate: topic %s exhausted/failed re-integrate → "
                     "failed-integration", tid)
        return tid

    # 5. No in-flight integrate, no envelope, attempts remain → spawn a fresh
    #    DETACHED integrate (grace + backoff gate the cadence).
    if not _grace_elapsed(topic, now) or not _backoff_elapsed(db, tid, now):
        return None
    try:
        thread = db.get_thread(thread_id) or {}
    except Exception:
        return None
    if not _real_commits_ahead(thread):
        return None  # empty branch / lost worktree — not this driver's job

    proc = _spawn_detached_integrate(thread, db)
    n = _record_attempt(db, tid, now, proc)  # record even on spawn failure → backoff + backstop apply
    if proc is None:
        return None
    _log.info("reintegrate: spawned detached integrate for wedged topic %s (attempt %d)",
              tid, n)
    return tid


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
