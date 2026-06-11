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

from dbops import db_graph
from dbops.schema import _now

ARMED_PROJECT_KEY = "autopilot_armed_project"
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


def get_armed_project(db) -> str | None:
    """Armed project id from the settings table, or None when disarmed."""
    try:
        val = (db.get_setting(ARMED_PROJECT_KEY) or "").strip()
    except Exception:
        return None  # pre-migration DB without a settings table
    return val or None


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
    """One dispatcher tick for the armed project. Never raises.

    Per ready node: atomic claim → lazy thread (cap-aware: defer on
    MAX_THREADS/pool-full, retry next tick) → hydrated dispatch → running.
    Also runs the stale-claim sweep first. Returns a stats dict.
    """
    stats: dict = {"dispatched": [], "swept": [], "deferred": [], "errors": []}
    armed = get_armed_project(db)
    if not armed:
        return stats
    dispatch = dispatch_fn or _dispatch_via_pool

    try:
        stats["swept"] = sweep_stale_claims(db, armed)
        # Self-heal: promote any eligible pending nodes (idempotent) — covers a
        # completion that crashed between marking and ready-recompute.
        db_graph.recompute_ready(db, armed)
        ready = [n for n in db_graph.list_nodes(db, armed) if n["state"] == "ready"]
    except Exception:
        _log.exception("graph tick: ready-set scan failed — skipping tick")
        return stats

    for node in ready:
        node_id = node["id"]
        if get_armed_project(db) != armed:
            break  # disarmed mid-batch — stop claiming
        try:
            if not claim_node(db, node_id):
                continue  # another claimer won (DA B4)
            try:
                thread_id = db.create_thread(
                    f"[{node_id}] {node['title']}"[:80], session_id=_session_id(db)
                )
            except ValueError as e:
                db_graph.node_transition(db, node_id, "stale_reset")
                if "Maximum of" not in str(e):
                    # NOT the MAX_THREADS cap (DA round-2 minor 6, 2026-06-10:
                    # unrelated ValueErrors were silently deferred forever).
                    stats["errors"].append(node_id)
                    db.add_action_item(
                        thread_id=None,
                        message=(
                            f"⚠️ Autopilot thread creation failed for graph "
                            f"node {node_id}: {e}"
                        ),
                        type_="failure",
                        priority="high",
                    )
                    continue
                # MAX_THREADS cap — claim released, retry next tick.
                stats["deferred"].append(node_id)
                _log.info("graph tick: thread cap hit — node %s deferred", node_id)
                break  # cap is global; later nodes would hit it too
            db.update_thread(thread_id, project_id=armed)
            # Bind BEFORE send-task (DA round-2 MAJOR-4, 2026-06-10): a crash
            # in the dispatch window must leave the node thread-bound so the
            # stale sweep cannot reclaim it and double-dispatch the work.
            db_graph.set_node_thread(db, node_id, thread_id)
            fail_key = (str(db.db_path), node_id)
            try:
                dispatch(db, thread_id, _hydrate_for_node(db, armed, node), node)
            except CapacityError:
                db.archive_thread(thread_id)
                db_graph.set_node_thread(db, node_id, None)
                db_graph.node_transition(db, node_id, "stale_reset")
                stats["deferred"].append(node_id)
                break
            except Exception as e:
                db.archive_thread(thread_id)
                db_graph.set_node_thread(db, node_id, None)
                stats["errors"].append(node_id)
                fails = _dispatch_fails.get(fail_key, 0) + 1
                _dispatch_fails[fail_key] = fails
                if fails >= MAX_DISPATCH_FAILS:
                    _dispatch_fails.pop(fail_key, None)
                    _give_up_dispatch(db, node_id, e)
                else:
                    db_graph.node_transition(db, node_id, "stale_reset")
                    db.add_action_item(
                        thread_id=None,
                        message=(
                            f"⚠️ Autopilot dispatch failed for graph node "
                            f"{node_id} (attempt {fails}/{MAX_DISPATCH_FAILS}): {e}"
                        ),
                        type_="failure",
                        priority="high",
                    )
                continue
            _dispatch_fails.pop(fail_key, None)
            db_graph.node_transition(db, node_id, "dispatch")  # → running
            db.add_notification_v2(
                thread_id=thread_id,
                message=f"⬢ autopilot dispatched graph node {node_id} — {node['title']}",
                session_id=_session_id(db),
            )
            stats["dispatched"].append(node_id)
        except Exception:
            # Belt-and-braces: a tick must never take the watchdog down.
            _log.exception("graph tick: unexpected error on node %s", node_id)
            stats["errors"].append(node_id)
    return stats


def _session_id(db) -> str:
    with db._connect() as conn:
        return db._get_session_key(conn, "session_id") or ""

