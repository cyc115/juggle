"""juggle_graph_dispatch — watchdog-owned graph dispatcher (autopilot Phase 2).

Owns: the per-tick claim→hydrate→dispatch loop for the armed project
(``graph_tick``), the atomic ready→dispatching claim (the one sanctioned
``graph_nodes.state`` writer besides ``dbops.db_graph.node_transition`` —
a compare-and-swap cannot go through read-then-write), the stale-claim
sweep.
Must not own: hydration (juggle_graph_hydration — re-exported here for
callers), node state semantics (dbops.db_graph), completion marking
(juggle_cmd_agents_graph), or the watchdog poll loop (which only calls
``graph_tick`` and must never crash because of it).

The watchdog tick is the SOLE dispatcher (DA B4/M1): complete-agent only
marks; arming is the ``autopilot_armed_project`` settings key (authority:
settings table, DA M6 — the toggle command itself lands in Phase 4).
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone

from dbops import db_graph, db_topics
from dbops.schema import _now
from juggle_autopilot_state import (  # noqa: F401 — re-exported, existing importers
    ARMED_PROJECT_KEY,
    get_armed_project,
    get_armed_projects,
)

STALE_CLAIM_SECS = 600  # dispatching >10 min with no thread → back to ready
NODE_ROLE = "coder"
# Retry cap (DA round-2 minor 1, 2026-06-10): a permanently broken dispatch
# path reset the node to ready every tick — one HIGH action item per tick
# forever. After this many consecutive failures the node goes failed-exec.
MAX_DISPATCH_FAILS = 3
_dispatch_fails: dict[tuple[str, str], int] = {}  # (db_path, node_id) → count

_log = logging.getLogger("juggle-graph-dispatch")


class CapacityError(RuntimeError):
    """Thread/agent capacity hit — defer quietly and retry next tick."""


def claim_node(db, node_id: str) -> bool:
    """Atomic ready→dispatching claim (DA B4). True iff THIS caller won.

    Single conditional UPDATE; rowcount==1 is the claim token. Any concurrent
    claimer sees rowcount==0 because the row no longer matches state='ready'.
    """
    with db._connect() as conn:
        cur = conn.execute(
            "UPDATE graph_nodes SET state='dispatching', updated_at=? "
            "WHERE id=? AND state='ready'",
            (_now(), node_id),
        )
        conn.commit()
        return cur.rowcount == 1


def sweep_stale_claims(db, project_id: str) -> list[str]:
    """Reset crashed claims: dispatching >10 min with no thread → ready.

    Crash-safe + idempotent: a dispatcher that died between claim and
    send-task never set thread_id, so the node is reclaimable next tick.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_SECS)
    ).isoformat()
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id FROM graph_nodes WHERE project_id=? AND state='dispatching' "
            "AND thread_id IS NULL AND updated_at < ?",
            (project_id, cutoff),
        ).fetchall()
    stale = [r["id"] for r in rows]
    for node_id in stale:
        db_graph.node_transition(db, node_id, "stale_reset")
        _log.warning("graph dispatch: stale claim swept, node %s → ready", node_id)
    return stale


# ── hydration (extracted; re-exported for existing imports) ───────────────────

from juggle_graph_hydration import (  # noqa: E402, F401
    build_hydration,
    hydrate_for_node as _hydrate_for_node,
)


# ── dispatch path ──────────────────────────────────────────────────────────────


def _dispatch_via_pool(db, thread_id: str, prompt: str, node: dict) -> None:
    """Dispatch ``prompt`` for ``thread_id`` through the existing CLI path:
    cmd_get_agent (idle reuse or spawn) + cmd_send_task (worktree guard,
    template, tmux). Raises CapacityError (pool full → defer) or RuntimeError.
    """
    import contextlib
    import io
    from argparse import Namespace

    from juggle_cmd_agents import cmd_get_agent, cmd_send_task

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cmd_get_agent(
                Namespace(
                    thread_id=thread_id, role=NODE_ROLE, model=None,
                    repo=None, harness=None, fresh=False,
                    db_path=str(db.db_path),
                )
            )
    except SystemExit:
        out = buf.getvalue()
        if "pool full" in out.lower():
            raise CapacityError(f"agent pool full for node {node['id']}")
        raise RuntimeError(f"agent acquisition failed: {out.strip()}")

    agent = db.get_agent_by_thread(thread_id)
    if not agent:
        raise RuntimeError(f"no agent bound to thread {thread_id} after get-agent")

    with tempfile.NamedTemporaryFile(
        "w", suffix=f"-{node['id']}.md", prefix="juggle-graph-", delete=False
    ) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        cmd_send_task(
            Namespace(
                agent_id=agent["id"], prompt_file=prompt_file,
                no_template=False, worktree_path=None, worktree_branch=None,
                main_repo_path=None, allow_main=False,
                force_node=True,  # the tick IS the sanctioned dispatcher
                db_path=str(db.db_path),
            )
        )
    except BaseException as e:
        # Release the agent on ANY failure (DA round-2 minor 2, 2026-06-10:
        # only SystemExit released it — other exceptions leaked the agent
        # 'busy' on an archived thread forever).
        db.update_agent(agent["id"], status="idle", assigned_thread=None)
        if isinstance(e, SystemExit):
            raise RuntimeError(
                f"send-task failed for node {node['id']} (exit {e.code})"
            )
        raise
    finally:
        import os

        try:
            os.unlink(prompt_file)
        except OSError:
            pass


def _give_up_dispatch(db, node_id: str, err: Exception) -> None:
    """Retry cap reached: node → failed-exec + propagation + ONE final item
    (DA round-2 minor 1, 2026-06-10 — no more per-tick action-item flood)."""
    db_graph.mark_exec_failed(db, node_id)
    blocked = db_graph.propagate_failure(db, node_id)
    detail = f" Dependents blocked: {', '.join(blocked)}." if blocked else ""
    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ Autopilot gave up on graph node {node_id} after "
            f"{MAX_DISPATCH_FAILS} consecutive dispatch failures: {err}.{detail} "
            f"Fix the dispatch path, then reload the graph spec to resume."
        ),
        type_="failure",
        priority="high",
    )
    _log.error(
        "graph tick: node %s failed-exec after %d dispatch failures",
        node_id, MAX_DISPATCH_FAILS,
    )


# ── the tick ───────────────────────────────────────────────────────────────────


def graph_tick(db, mgr=None, *, dispatch_fn=None) -> dict:
    """One dispatcher tick across ALL armed projects, claiming TOPICS (R9).

    Per project: topic stale-claim sweep + topic-ready recompute (a failure
    skips ONLY that project — R4). Ready topics are ordered fairly
    (juggle_graph_scheduler) then dispatched through the claim → thread →
    hydrate → dispatch body. ONE thread per topic (MAX_THREADS bounds concurrent
    topics; integrate runs once per topic). Never raises.
    """
    from juggle_graph_hydration import hydrate_for_topic
    from juggle_graph_scheduler import interleave_ready
    from juggle_graph_status import IN_FLIGHT_STATES

    stats: dict = {"dispatched": [], "swept": [], "deferred": [], "errors": []}
    armed = get_armed_projects(db)
    if not armed:
        return stats
    dispatch = dispatch_fn or _dispatch_via_pool

    ready_by_project: dict[str, list[dict]] = {}
    in_flight: dict[str, int] = {}
    for pid in armed:
        try:
            stats["swept"] += sweep_stale_topic_claims(db, pid)
            db_topics.recompute_topic_ready(db, pid)
            topics = db_topics.list_topics(db, pid)
        except Exception:
            _log.exception(
                "graph tick: ready-set scan failed for %s — skipping project", pid
            )
            continue
        ready_by_project[pid] = [t for t in topics if t["state"] == "ready"]
        in_flight[pid] = sum(1 for t in topics if t["state"] in IN_FLIGHT_STATES)

    for pid, topic in interleave_ready(ready_by_project, in_flight, armed):
        tid = topic["id"]
        if pid not in get_armed_projects(db):
            continue  # THIS project disarmed mid-batch — others keep going
        try:
            if not claim_topic(db, tid):
                continue  # another claimer won (DA B4)
            try:
                thread_id = db.create_thread(
                    f"[{tid}] {topic['title']}"[:80], session_id=_session_id(db)
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
            db.add_notification_v2(
                thread_id=thread_id,
                message=f"⬢ autopilot dispatched topic {tid} — {topic['title']}",
                session_id=_session_id(db),
            )
            stats["dispatched"].append(tid)
        except Exception:
            _log.exception("graph tick: unexpected error on topic %s", tid)
            stats["errors"].append(tid)

    # ── Legacy flat-node fallback (R9/R6) ─────────────────────────────────────
    # A project whose graph has graph_nodes but 0 graph_topics (e.g. pre-3-tier
    # spec, or migration 37 backfilled 0 rows) produces no ready topics above,
    # so the loop skips it entirely and the build stalls.  Detect this case and
    # dispatch ready nodes directly via the existing node claim/hydrate path
    # (2026-06-11 bug J).
    _dispatch_flat_node_fallback(db, armed, stats, dispatch)

    return stats


def _dispatch_flat_node_fallback(
    db, armed: list[str], stats: dict, dispatch
) -> None:
    """Dispatch ready graph_nodes for projects that have no graph_topics."""
    from juggle_graph_hydration import hydrate_for_node
    from dbops import db_topics as _dt

    for pid in armed:
        if pid not in get_armed_projects(db):
            continue
        try:
            if _dt.list_topics(db, pid):
                continue  # project has topics — topic path owns dispatch
            stats["swept"] += sweep_stale_claims(db, pid)
            db_graph.recompute_ready(db, pid)
            nodes = [n for n in db_graph.list_nodes(db, pid) if n["state"] == "ready"]
        except Exception:
            _log.exception(
                "graph tick (flat fallback): ready-set scan failed for %s", pid
            )
            continue
        for node in nodes:
            if pid not in get_armed_projects(db):
                break
            node_id = node["id"]
            fail_key = (str(db.db_path), node_id)
            try:
                if not claim_node(db, node_id):
                    continue
                try:
                    thread_id = db.create_thread(
                        f"[{node_id}] {node['title']}"[:80],
                        session_id=_session_id(db),
                    )
                except ValueError as e:
                    db_graph.node_transition(db, node_id, "stale_reset")
                    if "Maximum of" not in str(e):
                        stats["errors"].append(node_id)
                    else:
                        stats["deferred"].append(node_id)
                        _log.info("graph tick (flat): thread cap — node %s deferred", node_id)
                    continue
                db.update_thread(thread_id, project_id=pid)
                db_graph.set_node_thread(db, node_id, thread_id)
                try:
                    dispatch(db, thread_id, hydrate_for_node(db, pid, node), node)
                except CapacityError:
                    db.archive_thread(thread_id)
                    db_graph.bind_thread(db, node_id, None)
                    db_graph.node_transition(db, node_id, "stale_reset")
                    stats["deferred"].append(node_id)
                    break
                except Exception as e:
                    db.archive_thread(thread_id)
                    db_graph.bind_thread(db, node_id, None)
                    stats["errors"].append(node_id)
                    fails = _dispatch_fails.get(fail_key, 0) + 1
                    _dispatch_fails[fail_key] = fails
                    if fails >= MAX_DISPATCH_FAILS:
                        _dispatch_fails.pop(fail_key, None)
                        _give_up_dispatch(db, node_id, e)
                    else:
                        db_graph.node_transition(db, node_id, "stale_reset")
                    continue
                _dispatch_fails.pop(fail_key, None)
                db_graph.node_transition(db, node_id, "dispatch")
                db.add_notification_v2(
                    thread_id=thread_id,
                    message=f"⬢ autopilot dispatched node {node_id} — {node['title']}",
                    session_id=_session_id(db),
                )
                stats["dispatched"].append(node_id)
            except Exception:
                _log.exception("graph tick (flat fallback): error on node %s", node_id)
                stats["errors"].append(node_id)


def _session_id(db) -> str:
    with db._connect() as conn:
        return db._get_session_key(conn, "session_id") or ""


# Topic claim/sweep/give-up live in juggle_graph_dispatch_topics (LOC gate),
# re-exported here for graph_tick + callers/tests (bottom import breaks the cycle).
from juggle_graph_dispatch_topics import (  # noqa: E402, F401
    _give_up_topic_dispatch, claim_topic, sweep_stale_topic_claims)

