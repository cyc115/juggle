#!/usr/bin/env python3
"""Juggle CLI — Agent pool management commands."""

import json
import shutil
import sys
import threading
import time
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
    label = thread.get("label") or args.thread_id
    print(f"Thread {label} agent task set: {args.task_id}")


def cmd_complete_agent(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    label = thread.get("label") or args.thread_id

    db.update_thread(thread_uuid, agent_result=args.result_summary, status="done", reviewed=0)

    # Store the agent result as an assistant message so it's visible in get_last_exchange.
    if args.result_summary:
        db.add_message(thread_uuid, role="assistant", content=args.result_summary)

    # Auto-generate summary if the thread has none yet
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

    notification = (
        f"[Topic {label} completed] {thread['topic']} — results ready. "
        f"Use: python juggle_cli.py switch-thread {label}"
    )
    db.add_notification(thread_uuid, notification)

    # Explicit retain — only if --retain provided; warn to stderr if omitted
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
    else:
        print(
            "Warning: no --retain provided. Pass --retain to preserve useful context.",
            file=sys.stderr,
        )

    print(f"Thread {label} agent completed.")


def cmd_fail_agent(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    label = thread.get("label") or args.thread_id
    db.update_thread(thread_uuid, status="failed", agent_result=args.error)
    notification = f"[Topic {label} failed] {thread['topic']} — {args.error}"
    db.add_notification(thread_uuid, notification)

    print(f"Thread {label} agent failed.")


def cmd_check_agents(_):
    db = get_db()
    threads = db.get_all_threads()
    background = [
        {"thread_id": t.get("label") or t["id"][:8], "task_id": t.get("agent_task_id", ""), "topic": t["topic"]}
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


def cmd_list_agents(_):
    db = get_db()
    agents = db.get_all_agents()
    if not agents:
        print("No agents.")
        return

    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    mgr = JuggleTmuxManager()

    STATUS_EMOJI = {
        "idle": "💤",
        "busy": "🟢",
        "decommission_pending": "⚠️",
    }
    now = datetime.now(timezone.utc)
    idle_count = sum(1 for a in agents if a["status"] == "idle")
    term_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    sep = "─" * min(term_width, 100)

    print(f"Agents ({len(agents)} total, {idle_count} idle)")
    print(sep)
    for a in agents:
        emoji = STATUS_EMOJI.get(a["status"], "❓")
        idle_hint = ""
        if a["status"] == "busy":
            last_used = mgr.get_pane_last_used(a["pane_id"])
            if last_used and (time.time() - last_used) > JUGGLE_IDLE_THRESHOLD_SECS:
                emoji = "⏸️"
                idle_hint = " waiting?"
        short_id = a["id"][:8]
        role = a["role"]
        pane = a["pane_id"]
        last_active = a.get("last_active") or ""
        age = "-"
        if last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                secs = int((now - dt).total_seconds())
                age = f"{secs}s" if secs < 60 else (f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h")
            except (ValueError, TypeError):
                pass
        topic_str = "-"
        if a.get("assigned_thread"):
            t = db.get_thread(a["assigned_thread"])
            if t:
                lbl = t.get("label") or ""
                ttl = t.get("title") or " ".join(t["topic"].split()[:5])
                full = f"{lbl}: {ttl}" if lbl else ttl
                topic_str = full[:35]
        domain_str = a.get("domain") or "-"
        print(
            f"{emoji} [{short_id}]  {role:<12}  pane={pane:<6}  "
            f"domain={domain_str:<10}  topic={topic_str:<35}  age={age}{idle_hint}"
        )
    print(sep)


def cmd_get_agent(args):
    db = get_db()
    db.init_db()
    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    from juggle_db import MAX_BACKGROUND_AGENTS

    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    mgr = JuggleTmuxManager()

    # Purge dead panes and 24h-stale idle agents before pool-full check.
    now_ts = datetime.now(timezone.utc)
    for a in db.get_all_agents():
        if a["status"] != "idle":
            continue
        if not mgr.verify_pane(a["pane_id"]):
            print(f"[juggle] Dead pane detected ({a['pane_id']}), removing agent {a['id'][:8]}.", file=sys.stderr)
            db.delete_agent(a["id"])
            continue
        last_active = a.get("last_active") or ""
        if last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now_ts - dt).total_seconds() > _AGENT_TTL_SECS:
                    print(f"[juggle] Agent {a['id'][:8]} idle >24h, decommissioning.", file=sys.stderr)
                    mgr.decommission_agent(db, a["id"])
                    continue
            except (ValueError, TypeError):
                pass

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
            label = thread.get("label") or assigned[:8]
            db.update_thread(assigned, status="failed")
            db.add_notification(assigned,
                f"[Topic {label} failed] Agent released without completing.")

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
    release_line = f"\npython3 {SRC_DIR}/juggle_cli.py release-agent {args.agent_id}"
    full_prompt = prompt.rstrip() + release_line

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(args.agent_id, last_active=now)
    mgr.send_task(pane_id, full_prompt, is_new=is_new)
    print(f"Task sent to agent {args.agent_id[:8]} (pane {pane_id}).")
