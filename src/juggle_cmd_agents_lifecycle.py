"""
juggle_cmd_agents_lifecycle — Agent pool lifecycle commands.

Owns: cmd_get_agent, cmd_release_agent, cmd_decommission_agent.
Must not own: spawn/list/status (pool), task dispatch, completion/failure
handlers, worktree logic.

All shared symbols are accessed through juggle_cmd_agents_common (_com) so
that tests can monkeypatch _com.<symbol> and have the patches take effect.
"""

import json
import sys
from datetime import datetime, timezone

import juggle_cmd_agents_common as _com
from dbops import event_kinds as _ek
from dbops import db_topics as _tp
from dbops import terminal_states as _terminal_states


def cmd_get_agent(args):
    _db_path = getattr(args, "db_path", None)
    db = _com.get_db(db_path=_db_path) if isinstance(_db_path, str) else _com.get_db()
    db.init_db()
    sys.path.insert(0, str(_com.SRC_DIR))

    thread_uuid = _com._resolve_thread(db, args.thread_id)

    # Snapshot existing agent IDs so we can detect whether acquire_agent spawned
    # a new one (for the " new" suffix in output).
    existing_ids = {a["id"] for a in db.get_all_agents()}

    import juggle_dispatch_core as _dc
    import juggle_agent_reclaim as _reclaim
    from juggle_graph_dispatch import CapacityError
    from juggle_db import MAX_BACKGROUND_AGENTS

    def _acquire():
        return _dc.acquire_agent(
            db,
            thread_uuid,
            role=args.role,
            model=getattr(args, "model", None),
            repo=getattr(args, "repo", None),
            harness=getattr(args, "harness", None),
            fresh=getattr(args, "fresh", False),
            effort=getattr(args, "effort", None),
        )

    def _pool_full():
        print(
            f"Error: Agent pool full ({MAX_BACKGROUND_AGENTS} max). Wait for one to finish."
        )
        sys.exit(1)

    try:
        agent = _acquire()
    except CapacityError:
        # Pool at cap: reclaim ONE reclaimable idle agent (LRU) to free a slot
        # and retry acquisition exactly ONCE (2026-07-07: idle researchers must
        # not block `agent get --role coder`). If NOTHING is reclaimable — all
        # agents busy, or the only idle ones own unmerged worktree work — the
        # pool is genuinely full: fail loud rather than destroy work.
        if not _reclaim.reclaim_one_idle_agent(db):
            _pool_full()
        try:
            agent = _acquire()
        except CapacityError:
            _pool_full()
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    is_new = agent["id"] not in existing_ids
    if is_new:
        print(
            f"[juggle] No idle agent available, spawned new agent {agent['id'][:8]}.",
            file=sys.stderr,
        )
    suffix = " new" if is_new else ""
    print(f"{agent['id']} {agent['pane_id']}{suffix}")


def cmd_release_agent(args):
    db = _com.get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        # Fallback: treat arg as thread label/id and find its assigned agent
        try:
            thread_uuid = _com._resolve_thread(db, args.agent_id)
            agent = db.get_agent_by_thread(thread_uuid)
        except SystemExit:
            agent = None
    if agent is None:
        return  # no-op for unknown agent

    if agent["status"] == "decommission_pending":
        sys.path.insert(0, str(_com.SRC_DIR))
        from juggle_tmux import JuggleTmuxManager

        JuggleTmuxManager().decommission_agent(db, agent["id"])
        print(f"Agent {agent['id'][:8]} decommissioned.")
        return

    assigned = agent.get("assigned_thread")

    # Guard: block release if thread is still active unless --force
    if not getattr(args, "force", False) and assigned:
        thread = db.get_thread(assigned)
        if thread and thread["state"] not in ("done", "failed-exec", "archived"):
            label = thread.get("user_label") or assigned[:8]
            print(
                f"Error: Thread {label} is still active ({thread.get('state')}). "
                f"Call complete-agent or fail-agent first. Use --force to override (operator only)."
            )
            sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    agent_id = agent["id"]
    if assigned:
        context = json.loads(agent.get("context_threads") or "[]")
        if assigned not in context:
            context.append(assigned)
        context = context[-10:]
        db.update_agent(
            agent_id,
            status="idle",
            assigned_thread=None,
            context_threads=context,
            last_active=now,
        )
    else:
        db.update_agent(agent_id, status="idle", last_active=now)

    # Copy dispatch payload to thread before the agent record is cleared.
    # P8 Task 4.2: route through update_thread so the conversation node is
    # mirrored — get_thread now READS the node, so a raw `threads` UPDATE would
    # leave the read path stale.
    if assigned:
        agent_snap = db.get_agent(agent_id)
        if agent_snap:
            db.update_thread(
                assigned,
                last_dispatched_task=agent_snap.get("last_task"),
                last_dispatched_role=agent_snap.get("role"),
                last_dispatched_model=agent_snap.get("model"),
            )

    # Clear task state so a re-pooled agent doesn't carry stale last_task
    # into its next assignment — prevents watchdog from replaying a previous
    # thread's task during recovery.  model=None prevents a poisoned/typo model
    # value from being reused on the next dispatch (2026-06-11 bug F).
    db.update_agent(
        agent_id,
        last_task=None,
        last_send_task_pane_hash=None,
        last_send_task_at=None,
        watchdog_retried=0,
        model=None,
    )

    # Reconcile: if the agent's thread is still "background", it was released
    # without completing — mark the thread as failed so it doesn't appear stuck.
    # EXCEPT two routine post-completion cleanup cases (2026-07-03/04 incidents,
    # integrate-wedge #2) — release then is pool cleanup, NOT abandonment:
    #   1. (T-fix-release-after-verified-false-alarm) the bound topic is already
    #      verified+merged — its work landed. A topic still 'integrating' is NOT
    #      terminal and must keep the loud path.
    #   2. the durable completion ledger records a completion for this thread — a
    #      spool-applied agent_complete closes an agent_runs row to
    #      status='completed', which may not yet be reflected in the conversation
    #      node's state. Reading thread state alone marked provably-finished
    #      agents failed + filed spurious HIGH escalations.
    if assigned:
        thread = db.get_thread(assigned)
        if thread and thread["state"] == "background":
            topic = _tp.get_topic_by_thread(db, assigned)
            topic_terminal = _terminal_states.topic_work_landed(topic)
            label = thread.get("user_label") or thread.get("label") or assigned[:8]
            # Newest run only (get_runs is id-DESC): a completed CURRENT dispatch
            # means the agent finished; a still-open ('dispatched') or 'failed'
            # newest run means it did not. Checking the newest — not "any" —
            # avoids a stale completed run from a PRIOR dispatch masking a
            # genuinely-stuck re-dispatch.
            _runs = db.get_runs(thread_id=assigned, limit=1)
            completed = bool(_runs) and _runs[0].get("status") == "completed"
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM session WHERE key = 'session_id'"
                ).fetchone()
            session_id = row["value"] if row else ""
            if topic_terminal or completed:
                # Routine cleanup: no failed mark, no HIGH item, INFO notice only.
                db.emit_event(
                    assigned,
                    f"[Topic {label}] Agent released after completion — routine cleanup.",
                    session_id=session_id,
                    kind=_ek.WATCHDOG_RECOVERY,
                )
            else:
                db.update_thread(assigned, status="failed")
                db.add_action_item(
                    thread_id=assigned,
                    message=f"⚠️ [{label}] Agent released without completing — investigate and re-dispatch",
                    type_="failure",
                    priority="high",
                )
                db.emit_event(
                    assigned,
                    f"[Topic {label} failed] Agent released without completing.",
                    session_id=session_id,
                    kind=_ek.AGENT_FAILURE,
                )

    print(f"Agent {agent_id[:8]} released.")


def cmd_decommission_agent(args):
    db = _com.get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        print(f"Error: Agent {args.agent_id} not found.")
        sys.exit(1)
    sys.path.insert(0, str(_com.SRC_DIR))
    from juggle_tmux import JuggleTmuxManager

    JuggleTmuxManager().decommission_agent(db, args.agent_id)
    print(f"Agent {args.agent_id[:8]} decommissioned.")
