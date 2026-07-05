"""juggle_graph_dispatch — watchdog-owned graph dispatcher (autopilot Phase 2).

Owns: the per-tick claim→hydrate→dispatch loop for ALL projects (``graph_tick``),
the atomic ready→dispatching claim (the one sanctioned ``graph_tasks.state``
writer besides ``dbops.db_graph.task_transition`` — a compare-and-swap cannot
go through read-then-write), the stale-claim sweep.
Must not own: hydration (juggle_graph_hydration — re-exported here for
callers), task state semantics (dbops.db_graph), completion marking
(juggle_cmd_agents_graph), or the watchdog poll loop (which only calls
``graph_tick`` and must never crash because of it).

The watchdog tick is the SOLE dispatcher (DA B4/M1): complete-agent only
marks. Per-project arming is REMOVED (P7): the tick processes ready task/
research nodes across ALL projects. Conversation and legacy plain threads are
never auto-dispatched.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from dbops import db_graph, db_topics, db_topics_marking
from dbops import event_kinds as _ek
from dbops.schema import _now
from juggle_autopilot_state import (  # noqa: F401 — re-exported for compat
    ARMED_PROJECT_KEY,
    DISARMED_PROJECT_KEY,
    get_armed_project,
    get_armed_projects,
    get_disarmed_projects,
    select_armed,
)

STALE_CLAIM_SECS = 600  # dispatching >10 min with no thread → back to ready
TASK_ROLE = "coder"
# Retry cap (DA round-2 minor 1, 2026-06-10): a permanently broken dispatch
# path reset the task to ready every tick — one HIGH action item per tick
# forever. After this many consecutive failures the task goes failed-exec.
MAX_DISPATCH_FAILS = 3
_dispatch_fails: dict[tuple[str, str], int] = {}  # (db_path, task_id) → count

_log = logging.getLogger("juggle-graph-dispatch")


class CapacityError(RuntimeError):
    """Thread/agent capacity hit — defer quietly and retry next tick."""


def claim_task(db, task_id: str) -> bool:
    """Atomic ready→dispatching claim (DA B4). True iff THIS caller won.

    Single conditional UPDATE on ``nodes`` (the authoritative claim token, P8
    Task 4.1); rowcount==1 is the claim. Any concurrent claimer sees rowcount==0
    because the row no longer matches state='ready'.
    """
    from dbops.state_write import cas_state
    with db._connect() as conn:
        won = cas_state(conn, task_id, frm="ready", to="dispatching", now=_now())
        conn.commit()
        return won == 1


def sweep_stale_claims(db, project_id: str) -> list[str]:
    """Reset crashed claims: dispatching >10 min with no thread → ready.

    Crash-safe + idempotent: a dispatcher that died between claim and
    send-task never set the dispatch thread, so the task is reclaimable next
    tick. Stale == no kind='dispatch' node_edge bound (P8 M1/Q2).
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_SECS)
    ).isoformat()
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE kind='task' AND project_id=? "
            "AND state='dispatching' AND updated_at < ? "
            "AND id NOT IN (SELECT node_id FROM node_edges WHERE kind='dispatch')",
            (project_id, cutoff),
        ).fetchall()
    stale = [r["id"] for r in rows]
    for task_id in stale:
        db_graph.task_transition(db, task_id, "stale_reset")
        _log.warning("graph dispatch: stale claim swept, task %s → ready", task_id)
    return stale


# ── hydration (extracted; re-exported for existing imports) ───────────────────

from juggle_graph_hydration import (  # noqa: E402, F401
    build_hydration,
    hydrate_for_task as _hydrate_for_task,
)


# ── dispatch path ──────────────────────────────────────────────────────────────


def _dispatch_via_pool(db, thread_id: str, prompt: str, task: dict) -> None:
    """Dispatch ``prompt`` via dispatch_node(), resolving the role PER NODE
    (loop-entity Phase 2 — no longer hardcoded coder; researcher gets no
    worktree). Raises CapacityError (pool full → defer) or RuntimeError."""
    from juggle_dispatch_core import dispatch_node

    dispatch_node(db, thread_id, prompt, task, role=_resolve_dispatch_role(db, task))


def _give_up_dispatch(db, task_id: str, err: Exception) -> None:
    """Retry cap reached: task → failed-exec + propagation + ONE final item
    (DA round-2 minor 1, 2026-06-10 — no more per-tick action-item flood)."""
    db_graph.mark_exec_failed(db, task_id)
    blocked = db_graph.propagate_failure(db, task_id)
    detail = f" Dependents blocked: {', '.join(blocked)}." if blocked else ""
    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ Autopilot gave up on graph task {task_id} after "
            f"{MAX_DISPATCH_FAILS} consecutive dispatch failures: {err}.{detail} "
            f"Fix the dispatch path, then reload the graph spec to resume."
        ),
        type_="failure",
        priority="high",
    )
    _log.error(
        "graph tick: task %s failed-exec after %d dispatch failures",
        task_id, MAX_DISPATCH_FAILS,
    )


# ── the tick ───────────────────────────────────────────────────────────────────


def _all_project_ids(db) -> list[str]:
    """All project ids that have graph work (topics or tasks), ordered by
    last_active DESC (projects table) then alphabetically for unlisted ids."""
    try:
        with db._connect() as conn:
            # Priority-ordered ids from the projects table.
            proj_rows = conn.execute(
                "SELECT id FROM projects WHERE status='active' "
                "ORDER BY last_active DESC, id"
            ).fetchall()
            listed = [r[0] if not hasattr(r, "__getitem__") or isinstance(r, tuple) else r[0]
                      for r in proj_rows]
            # Also collect project ids only present in the node store (e.g. test
            # fixtures that create tasks/topics without inserting a projects row).
            # P8 c4-write-cut: discover from kind='task'/'topic' nodes, not the
            # retired graph_tasks/graph_topics tables.
            extra: set[str] = set()
            try:
                for r in conn.execute(
                    "SELECT DISTINCT project_id FROM nodes "
                    "WHERE kind IN ('task','topic') AND project_id IS NOT NULL"
                ).fetchall():
                    pid = r[0]
                    if pid and pid not in listed:
                        extra.add(pid)
            except Exception:
                pass
        return listed + sorted(extra)
    except Exception:
        return []


def graph_tick(db, mgr=None, *, dispatch_fn=None) -> dict:
    """One dispatcher tick across ALL projects, claiming TOPICS (R9).

    Per project: topic stale-claim sweep + topic-ready recompute (a failure
    skips ONLY that project — R4). Ready topics are ordered fairly
    (juggle_graph_scheduler) then dispatched through the claim → thread →
    hydrate → dispatch body. ONE thread per topic (MAX_THREADS bounds concurrent
    topics; integrate runs once per topic). Never raises.
    Per-project arming is REMOVED (P7): every project with ready task/research
    nodes is processed. Conversation nodes and legacy plain threads are never
    auto-dispatched.
    """
    from juggle_graph_hydration import hydrate_for_topic
    from juggle_graph_scheduler import interleave_ready
    from juggle_land_poller import poll_unlanded_topics
    from juggle_watchdog_rebase_nudge import sweep_rebase_nudges, clear_rebase_nudge

    stats: dict = {"dispatched": [], "swept": [], "deferred": [], "errors": [], "reconciled": []}
    # Reconcile orphaned in-flight task nodes BEFORE the ready-set scan (R1 wedge,
    # 2026-06-30): a kind='task' node stuck in {dispatching, running, integrating}
    # with a NULL/dead dispatch thread is healed off the wedge so it re-enters the
    # ready set this same tick. A node bound to a LIVE (busy) agent is never reset.
    try:
        from juggle_graph_reconcile import reconcile_orphans
        stats["reconciled"] = reconcile_orphans(db)
    except Exception:
        _log.exception("graph tick: orphan reconcile failed — continuing")
    stats["rebase_nudged"] = sweep_rebase_nudges(db, mgr) if mgr else []  # P3
    # Default-armed filter: drop disarmed projects (snapshot once per tick).
    # Empty disarmed set → unchanged list (behaviour-identical to drive-all).
    all_projects = select_armed(_all_project_ids(db), get_disarmed_projects(db))
    dispatch = dispatch_fn or _dispatch_via_pool

    # irl-repair T4: pre-claim repair sweep — a dispatchable repair wins the
    # last agent slot BEFORE the claim loop (conditional slot reservation).
    from juggle_graph_repair import run_repair_sweeps
    stats["repaired"] = run_repair_sweeps(db, all_projects, dispatch, _session_id(db))

    ready_by_project: dict[str, list[dict]] = {}
    in_flight: dict[str, int] = {}
    for pid in all_projects:
        try:
            stats["swept"] += sweep_stale_topic_claims(db, pid)
            poll_unlanded_topics(db, pid)  # SPEC §5.3/§7.5
            db_topics_marking.unblock_topic_dependents(db, pid)  # converge blocked-failed⇄open every tick, not only on lift (T-fix-topic-unblock-sweep 2026-07-04)
            db_topics.recompute_topic_ready(db, pid)  # raises → poisoned project skipped (R4 blast-radius pin)
            topics = db_topics.list_topics(db, pid)
        except Exception:
            _log.exception("graph tick: ready-set scan failed for %s — skipping project", pid)
            continue
        ready_by_project[pid] = [t for t in topics if t["state"] == "ready"]
        in_flight[pid] = sum(1 for t in topics if t["state"] in ("dispatching", "running", "integrating"))

    for pid, topic in interleave_ready(ready_by_project, in_flight, all_projects):
        tid = topic["id"]
        try:
            if not claim_topic(db, tid):
                continue  # another claimer won (DA B4)
            # Dedup defect F (2026-07-01): reuse the topic's already-bound
            # surfacing conversation (named via add-task --topic) instead of
            # minting a second "[T-<id>]" mirror row. Reuse it even when archived,
            # so an explicitly-archived surface is never resurrected as a fresh
            # mirror — ONE conversation row per task, ever.
            # Reuse the topic's bound surfacing thread, UNLESS it is gone or is
            # already driving another active topic (F2 fan-in guard) — see
            # reusable_thread. None here → create + rebind a fresh thread below.
            thread_id = reusable_thread(db, topic.get("thread_id"), tid)
            try:
                if thread_id is None:
                    thread_id = db.create_thread(
                        f"[{tid}] {topic['title']}"[:80],
                        session_id=_session_id(db),
                        project_id=pid,
                    )
            except ValueError as e:
                db_topics.topic_transition(db, tid, "stale_reset")
                if "Maximum of" not in str(e):
                    stats["errors"].append(tid)
                    db.add_action_item(
                        thread_id=None,
                        message=(f"⚠️ Autopilot thread creation failed for "
                                 f"topic {tid}: {e}"),
                        type_="failure", priority="high",
                    )
                    continue
                stats["deferred"].append(tid)
                _log.info("graph tick: thread cap hit — topic %s deferred", tid)
                break  # cap is global; later topics would hit it too
            db.update_thread(thread_id, project_id=pid)
            # Record the cyc_ branch at dispatch (T-verified-merged-sha, hole
            # #3): tick-dispatched topics previously left worktree_branch ''
            # so integrate couldn't know which branch tip to record as
            # merged_sha. The branch matches _create_worktree's deterministic
            # f"cyc_{thread_label}" (thread_label = user_label or id[:6]).
            _t = db.get_thread(thread_id) or {}
            _label = (_t.get("user_label") or thread_id[:6])
            db.update_thread(thread_id, worktree_branch=f"cyc_{_label}")
            # Bind BEFORE send-task (DA round-2 MAJOR-4): a crash in the
            # dispatch window must leave the topic thread-bound so the stale
            # sweep cannot reclaim and double-dispatch it.
            db_topics.set_topic_thread(db, tid, thread_id)
            fail_key = (str(db.db_path), tid)
            try:
                dispatch(db, thread_id, hydrate_for_topic(db, pid, topic), topic)
            except CapacityError:
                db.archive_thread(thread_id)
                db_topics.set_topic_thread(db, tid, None)
                db_topics.topic_transition(db, tid, "stale_reset")
                stats["deferred"].append(tid)
                break
            except Exception as e:
                db.archive_thread(thread_id)
                db_topics.set_topic_thread(db, tid, None)
                stats["errors"].append(tid)
                fails = _dispatch_fails.get(fail_key, 0) + 1
                _dispatch_fails[fail_key] = fails
                if fails >= MAX_DISPATCH_FAILS:
                    _dispatch_fails.pop(fail_key, None)
                    _give_up_topic_dispatch(db, tid, e)
                else:
                    db_topics.topic_transition(db, tid, "stale_reset")
                    db.add_action_item(
                        thread_id=None,
                        message=(f"⚠️ Autopilot dispatch failed for topic {tid} "
                                 f"(attempt {fails}/{MAX_DISPATCH_FAILS}): {e}"),
                        type_="failure", priority="high",
                    )
                continue
            _dispatch_fails.pop(fail_key, None)
            db_topics.topic_transition(db, tid, "dispatch")  # → running
            clear_rebase_nudge(db, tid)  # fresh running episode → nudge gate reopens
            db.emit_event(
                thread_id=thread_id, session_id=_session_id(db), kind=_ek.AUTOPILOT_DISPATCH,
                message=f"⬢ autopilot dispatched topic {tid} — {topic['title']}",
            )
            stats["dispatched"].append(tid)
        except Exception:
            _log.exception("graph tick: unexpected error on topic %s", tid)
            stats["errors"].append(tid)

    # ── Legacy flat-task fallback (R9/R6) ─────────────────────────────────────
    # A project whose graph has graph_tasks but 0 graph_topics (e.g. pre-3-tier
    # spec, or migration 37 backfilled 0 rows) produces no ready topics above,
    # so the loop skips it entirely and the build stalls.  Detect this case and
    # dispatch ready tasks directly via the existing task claim/hydrate path
    # (2026-06-11 bug J).
    _dispatch_flat_task_fallback(db, all_projects, stats, dispatch)

    return stats


def _session_id(db) -> str:
    with db._connect() as conn:
        return db._get_session_key(conn, "session_id") or ""


# Per-node dispatch-attribute resolvers live in juggle_graph_dispatch_resolve
# (LOC gate, loop-entity V2/P2), re-exported here so graph_tick + callers/tests
# keep importing ``_resolve_dispatch_role`` (and the V2/P2 ``_resolve_dispatch_model``)
# from this module unchanged (bottom import breaks the TASK_ROLE cycle).
from juggle_graph_dispatch_resolve import _resolve_dispatch_role  # noqa: E402, F401

# Topic claim/sweep/give-up live in juggle_graph_dispatch_topics (LOC gate),
# re-exported here for graph_tick + callers/tests (bottom import breaks the cycle).
from juggle_graph_dispatch_topics import (  # noqa: E402, F401
    _give_up_topic_dispatch, claim_topic, reusable_thread, sweep_stale_topic_claims)

# Legacy flat-task fallback lives in juggle_graph_dispatch_flat (LOC gate,
# loop-entity Phase 2), re-exported here so graph_tick + callers/tests keep
# calling ``_dispatch_flat_task_fallback`` unchanged (bottom import breaks the
# cycle — the flat module imports the claim/sweep/give-up helpers defined above).
from juggle_graph_dispatch_flat import _dispatch_flat_task_fallback  # noqa: E402, F401

