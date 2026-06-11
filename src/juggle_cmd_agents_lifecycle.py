"""
juggle_cmd_agents_lifecycle — Agent pool lifecycle commands.

Owns: cmd_get_agent, cmd_release_agent, cmd_decommission_agent.
Must not own: spawn/list/status (pool), task dispatch, completion/failure
handlers, worktree logic.

All shared symbols are accessed through juggle_cmd_agents_common (_com) so
that tests can monkeypatch _com.<symbol> and have the patches take effect.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import juggle_cmd_agents_common as _com


def cmd_get_agent(args):
    _db_path = getattr(args, "db_path", None)
    db = _com.get_db(db_path=_db_path) if isinstance(_db_path, str) else _com.get_db()
    db.init_db()
    sys.path.insert(0, str(_com.SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    from juggle_db import MAX_BACKGROUND_AGENTS

    thread_uuid = _com._resolve_thread(db, args.thread_id)
    mgr = JuggleTmuxManager()

    all_agents = db.get_all_agents()
    if len(all_agents) >= MAX_BACKGROUND_AGENTS:
        print(
            f"Error: Agent pool full ({MAX_BACKGROUND_AGENTS} max). Wait for one to finish."
        )
        sys.exit(1)

    # Resolve target repo for filtering (default: current cwd git toplevel)
    explicit_repo = getattr(args, "repo", None)
    target_repo = explicit_repo
    if target_repo is None:
        try:
            target_repo = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], text=True, cwd=os.getcwd()
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            target_repo = ""

    # Resolve requested harness: explicit --harness flag > config default
    agent_cfg = _com._get_settings().get("agent", {})
    requested_harness = getattr(args, "harness", None) or agent_cfg.get("harness") or "claude"

    # Walk ranked idle candidates; pick the first one whose pane is actually
    # ready at the Claude UI prompt (single-shot capture-pane check).  If none
    # of the idle agents are ready — the pane may still be rendering, mid-
    # shutdown, or have stray input — fall through to spawn a fresh agent.
    agent = None
    if not getattr(args, "fresh", False):
        for candidate in db.get_ranked_idle_agents(thread_uuid, role=args.role):
            # Filter by repo_path: NULL = pre-migration → incompatible; skip mismatched
            agent_repo = candidate.get("repo_path")
            if agent_repo is None:
                continue  # pre-migration agent, unknown repo — skip
            if target_repo and agent_repo != target_repo:
                continue  # mismatched repo — skip, don't decommission
            # hard role filter — role score (+1) is not enough to prevent a
            # wrong-role agent (e.g. planner) winning on context score (+2) for a coder request.
            if args.role and candidate.get("role") != args.role:
                continue
            if candidate.get("harness") != requested_harness:
                continue  # harness mismatch — spawn fresh on correct harness
            if mgr.wait_for_ready_to_paste(candidate["pane_id"], attempts=1):
                # Reset pane cwd so a stranded agent starts clean
                reset_dir = target_repo or os.path.expanduser("~")
                mgr._run_tmux("send-keys", "-t", candidate["pane_id"], f"cd {reset_dir}", "Enter")
                agent = candidate
                break
    is_new = agent is None

    if is_new:
        try:
            agent = mgr.spawn_agent(
                db, args.role or "researcher", model=getattr(args, "model", None),
                harness_override=requested_harness,
            )
            print(
                f"[juggle] No idle agent available, spawned new agent {agent['id'][:8]}.",
                file=sys.stderr,
            )
        except (RuntimeError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    _update_kw: dict = dict(
        status="busy", assigned_thread=thread_uuid, last_active=now, busy_since=now
    )
    _model_arg = getattr(args, "model", None)
    if _model_arg:
        _update_kw["model"] = _model_arg
    if explicit_repo:
        _update_kw["repo_path"] = target_repo
    db.update_agent(agent["id"], **_update_kw)
    db.update_thread(thread_uuid, status="background")

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
        if thread and thread["status"] not in ("closed", "failed", "archived"):
            label = thread.get("user_label") or assigned[:8]
            print(
                f"Error: Thread {label} is still active ({thread['status']}). "
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

    # Copy dispatch payload to thread before the agent record is cleared
    if assigned:
        agent_snap = db.get_agent(agent_id)
        if agent_snap:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE threads SET last_dispatched_task=?, last_dispatched_role=?, "
                    "last_dispatched_model=? WHERE id=?",
                    (
                        agent_snap.get("last_task"),
                        agent_snap.get("role"),
                        agent_snap.get("model"),
                        assigned,
                    ),
                )
                conn.commit()

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
    if assigned:
        thread = db.get_thread(assigned)
        if thread and thread["status"] == "background":
            label = thread.get("user_label") or thread.get("label") or assigned[:8]
            db.update_thread(assigned, status="failed")
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM session WHERE key = 'session_id'"
                ).fetchone()
            session_id = row["value"] if row else ""
            db.add_action_item(
                thread_id=assigned,
                message=f"⚠️ [{label}] Agent released without completing — investigate and re-dispatch",
                type_="failure",
                priority="high",
            )
            db.add_notification_v2(
                assigned,
                f"[Topic {label} failed] Agent released without completing.",
                session_id=session_id,
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
