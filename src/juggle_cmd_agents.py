#!/usr/bin/env python3
"""Juggle CLI — Agent pool management commands.

Agent completion protocol (embed in all dispatched prompts):
  When finished, call EXACTLY ONE of:
  1. Success:  juggle complete-agent <hex6> "<result>"
  2. Action needed: juggle request-action <hex6> "<what>" --type manual_step --priority high
  3. Failure:  juggle fail-agent <hex6> "<error>"
"""

import json
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from juggle_cli_common import (
    SRC_DIR,
    JUGGLE_IDLE_THRESHOLD_SECS,
    _get_hindsight_client,
    _last_sentences,
    _resolve_thread,
    get_db,
)
from juggle_settings import get_settings as _get_settings

_AGENT_TTL_SECS: int = _get_settings()["agent_idle_ttl_secs"]


def cmd_set_agent(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    db.update_thread(thread_uuid, agent_task_id=args.task_id, status="background")
    label = thread.get("user_label") or thread.get("label") or args.thread_id
    print(f"Thread {label} agent task set: {args.task_id}")


def cmd_complete_agent(args):
    """Mark agent complete: thread → closed, create notifications_v2 row,
    convert any open_questions to action_items."""
    import juggle_cli_common as _common
    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    # Current session id
    with db._connect() as conn:
        srow = conn.execute("SELECT value FROM session WHERE key = 'session_id'").fetchone()
    session_id = srow["value"] if srow else ""

    # 1. Convert any open_questions to action_items
    oq_raw = thread.get("open_questions") or "[]"
    try:
        open_questions = json.loads(oq_raw) if isinstance(oq_raw, str) else (oq_raw or [])
    except (json.JSONDecodeError, ValueError):
        open_questions = []
    for q in open_questions:
        if isinstance(q, dict):
            text = q.get("text") or q.get("question") or q.get("q") or str(q)
        else:
            text = str(q)
        db.add_action_item(
            thread_id=thread_uuid,
            message=text,
            type_="question",
            priority="normal",
        )
    if open_questions:
        db.update_thread(thread_uuid, open_questions="[]")

    # 2. Store the agent result as an assistant message
    if args.result_summary:
        db.add_message(thread_uuid, role="assistant", content=args.result_summary)

    # 3. Auto-generate summary if the thread has none yet
    if not (thread.get("summary") or "").strip():
        exchange = db.get_last_exchange(thread_uuid)
        raw_last_user = exchange.get("last_user") or ""
        is_junk = (
            raw_last_user.startswith("<task-notification")
            or "task-id" in raw_last_user
            or raw_last_user.strip().startswith("/")
        )
        if not is_junk:
            last_q = _last_sentences(raw_last_user, max_chars=80)
            last_a = _last_sentences(exchange.get("last_assistant") or "", max_chars=80)
            if last_q or last_a:
                auto_summary = f"{last_q} -> {last_a}" if (last_q and last_a) else (last_q or last_a)
                db.update_thread(thread_uuid, summary=auto_summary)

    # 4. Transition thread to closed
    db.set_thread_status(thread_uuid, "closed")

    # 5. Create notification row (informational, session TTL)
    title = thread.get("title") or thread.get("topic") or "thread"
    db.add_notification_v2(
        thread_id=thread_uuid,
        message=f"{title}: {args.result_summary}",
        session_id=session_id,
    )

    # 6. Optional retain text → Hindsight
    retain_text = getattr(args, "retain_text", None)
    if retain_text:
        def _do_retain(text, topic):
            client = _get_hindsight_client()
            if client:
                try:
                    client.retain(f"[{topic}] {text}", context="learnings")
                except Exception:
                    pass
        threading.Thread(
            target=_do_retain,
            args=(retain_text, thread.get("topic", "")),
            daemon=True,
        ).start()

    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)

    label = thread.get("user_label") or thread.get("label") or args.thread_id
    print(f"Agent complete for Topic {label} → closed. Notification logged.")


_TRANSIENT_PATTERNS = (
    "etimedout", "econnrefused", "econnreset", "timeout", "timed out",
    "rate limit", "429", "502", "503", "504",
    "network unreachable", "temporarily unavailable",
    "audio device", "audio busy",
)

_PERSISTENT_HINTS = (
    "401", "403", "unauthorized", "forbidden",
    "filenotfounderror", "no such file", "permissionerror",
    "syntaxerror", "typeerror", "valueerror",
    "assertionerror", "keyerror", "attributeerror",
)


def _classify_failure(error: str) -> str:
    """Return 'transient' or 'persistent'. Case-insensitive substring match."""
    if not error:
        return "persistent"
    low = error.lower()
    # Persistent hints take precedence when ambiguous
    for h in _PERSISTENT_HINTS:
        if h in low:
            return "persistent"
    for t in _TRANSIENT_PATTERNS:
        if t in low:
            return "transient"
    return "persistent"


def cmd_fail_agent(args):
    """Route agent failure: transient → leave running for retry; persistent → action_item + close."""
    import juggle_cli_common as _common
    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    # Explicit classification wins; else auto-classify
    ft = getattr(args, "failure_type", None)
    if ft not in ("transient", "persistent"):
        ft = _classify_failure(args.error or "")

    label = thread.get("user_label") or thread.get("label") or args.thread_id

    if ft == "transient":
        db.touch_last_active(thread_uuid)
        max_retries = getattr(args, "max_retries", 0)
        print(f"Transient failure on Topic {label}; thread stays 'running' "
              f"(max_retries={max_retries}). Error: {args.error}")
        return

    # Persistent
    db.add_action_item(
        thread_id=thread_uuid,
        message=f"agent failure: {args.error}",
        type_="failure",
        priority="high",
    )
    db.set_thread_status(thread_uuid, "closed")

    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)

    print(f"Persistent failure on Topic {label}; action_item created and thread → closed.")


def cmd_request_action(args):
    """Create an action_items row tied to a thread. Thread stays in current state;
    only last_active_at is touched."""
    import juggle_cli_common as _common
    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    priority = getattr(args, "priority", None) or "normal"
    type_ = getattr(args, "type", None) or "manual_step"
    if priority not in ("low", "normal", "high"):
        print(f"Error: priority must be low/normal/high, got {priority!r}")
        sys.exit(1)
    if type_ not in ("question", "manual_step", "decision", "failure"):
        print(f"Error: type must be question/manual_step/decision/failure, got {type_!r}")
        sys.exit(1)
    aid = db.add_action_item(
        thread_id=thread_uuid,
        message=args.message,
        type_=type_,
        priority=priority,
    )
    db.touch_last_active(thread_uuid)

    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)

    label = thread.get("user_label") or thread.get("label") or args.thread_id
    print(f"Action item #{aid} logged for Topic {label} (priority={priority}).")


def cmd_ack_action(args):
    """Dismiss an action item by id."""
    import juggle_cli_common as _common
    db = _common.get_db()
    db.dismiss_action_item(int(args.action_id))
    print(f"Action item #{args.action_id} dismissed.")


def cmd_list_actions(_):
    """Print open action items, newest high-priority first."""
    import juggle_cli_common as _common
    db = _common.get_db()
    items = db.get_open_action_items()
    if not items:
        print("No open action items.")
        return
    for it in items:
        thread_suffix = ""
        if it.get("thread_id"):
            t = db.get_thread(it["thread_id"])
            if t:
                lbl = t.get("user_label") or it["thread_id"][:6]
                thread_suffix = f" (thread [{lbl}])"
        print(f"⚡ [{it['id']}] {it['priority'].upper():6} {it['message']}{thread_suffix}")


def cmd_check_agents(_):
    db = get_db()
    threads = db.get_all_threads()
    background = [
        {"thread_id": t.get("user_label") or t["id"][:8], "task_id": t.get("agent_task_id", ""), "topic": t["topic"]}
        for t in threads
        if t["status"] == "background"
    ]
    print(json.dumps(background))


def cmd_spawn_agent(args):
    db = get_db()
    db.init_db()
    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    mgr = JuggleTmuxManager()
    try:
        agent = mgr.spawn_agent(db, args.role, model=getattr(args, "model", None))
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"{agent['id']} {agent['pane_id']}")


def cmd_list_agents(args):
    db = get_db()
    agents = db.get_all_agents()
    if not agents:
        print("No agents.")
        return

    now = datetime.now(timezone.utc)

    def _agent_age(last_active: str) -> str:
        if not last_active:
            return "-"
        try:
            dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            secs = int((now - dt).total_seconds())
            return f"{secs}s" if secs < 60 else (f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h")
        except (ValueError, TypeError):
            return "-"

    def _agent_topic_label(a) -> str:
        if a.get("assigned_thread"):
            t = db.get_thread(a["assigned_thread"])
            if t:
                return t.get("user_label") or t["id"][:6]
        return "-"

    for a in agents:
        short_id = a["id"][:8]
        role = a.get("role") or "-"
        status = a.get("status") or "-"
        pane = a.get("pane_id") or "-"
        topic_lbl = _agent_topic_label(a)
        age = _agent_age(a.get("last_active") or "")
        print(f"{short_id} {role:<8} {status:<5} {pane} [{topic_lbl}] {age}")


def cmd_get_agent(args):
    db = get_db()
    db.init_db()
    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    from juggle_db import MAX_BACKGROUND_AGENTS

    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    mgr = JuggleTmuxManager()

    # Purge stale agents
    from juggle_tmux import reap_stale_agents
    reap_stale_agents(db, mgr)

    all_agents = db.get_all_agents()
    if len(all_agents) >= MAX_BACKGROUND_AGENTS:
        print(f"Error: Agent pool full ({MAX_BACKGROUND_AGENTS} max). Wait for one to finish.")
        sys.exit(1)

    thread_domain = thread.get("domain") if thread else None
    if thread_domain is None:
        thread_domain = db.infer_domain_from_prompt(thread.get("topic", "") if thread else "")

    agent = db.get_best_agent(thread_uuid, role=args.role, domain=thread_domain)
    is_new = agent is None

    if is_new:
        try:
            agent = mgr.spawn_agent(db, args.role or "researcher", model=getattr(args, "model", None))
            print(f"[juggle] No idle agent available, spawned new agent {agent['id'][:8]}.", file=sys.stderr)
        except (RuntimeError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(agent["id"], status="busy", assigned_thread=thread_uuid,
                    last_active=now, domain=thread_domain)
    db.update_thread(thread_uuid, status="background")

    suffix = " new" if is_new else ""
    print(f"{agent['id']} {agent['pane_id']}{suffix}")


def cmd_release_agent(args):
    db = get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        return  # no-op for unknown agent

    if agent["status"] == "decommission_pending":
        sys.path.insert(0, str(SRC_DIR))
        from juggle_tmux import JuggleTmuxManager
        JuggleTmuxManager().decommission_agent(db, args.agent_id)
        print(f"Agent {args.agent_id[:8]} decommissioned.")
        return

    assigned = agent.get("assigned_thread")
    now = datetime.now(timezone.utc).isoformat()
    if assigned:
        context = json.loads(agent.get("context_threads") or "[]")
        if assigned not in context:
            context.append(assigned)
        context = context[-10:]
        db.update_agent(
            args.agent_id,
            status="idle",
            assigned_thread=None,
            context_threads=context,
            last_active=now,
        )
    else:
        db.update_agent(args.agent_id, status="idle", last_active=now)

    # Reconcile: if the agent's thread is still "background", it was released
    # without completing — mark the thread as failed so it doesn't appear stuck.
    if assigned:
        thread = db.get_thread(assigned)
        if thread and thread["status"] == "background":
            label = thread.get("user_label") or thread.get("label") or assigned[:8]
            db.update_thread(assigned, status="failed")
            db.add_notification(assigned,
                f"[Topic {label} failed] Agent released without completing.", severity="error")

    print(f"Agent {args.agent_id[:8]} released.")


def cmd_decommission_agent(args):
    db = get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        print(f"Error: Agent {args.agent_id} not found.")
        sys.exit(1)
    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    JuggleTmuxManager().decommission_agent(db, args.agent_id)
    print(f"Agent {args.agent_id[:8]} decommissioned.")


def cmd_send_task(args):
    db = get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        print(f"Error: Agent {args.agent_id} not found.")
        sys.exit(1)

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"Error: Prompt file {args.prompt_file} not found.")
        sys.exit(1)

    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    mgr = JuggleTmuxManager()

    pane_id = agent["pane_id"]

    if not mgr.verify_pane(pane_id):
        mgr.ensure_session()
        new_pane_id = mgr.spawn_pane()
        mgr.start_claude_in_pane(new_pane_id)
        db.update_agent(args.agent_id, pane_id=new_pane_id)
        pane_id = new_pane_id
        agent = db.get_agent(args.agent_id)
        is_new = True
    else:
        is_new = False

    prompt = prompt_path.read_text()
    full_prompt = prompt.rstrip()

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(args.agent_id, last_active=now)
    mgr.send_task(pane_id, full_prompt, is_new=is_new)
    print(f"Task sent to agent {args.agent_id[:8]} (pane {pane_id}).")
