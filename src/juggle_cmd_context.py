#!/usr/bin/env python3
"""Juggle CLI — Shared context, memory, domain, and misc commands."""

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from juggle_cli_common import (
    SRC_DIR,
    DB_PATH,
    _get_hindsight_client,
    _humanize_dt,
    _resolve_thread,
    get_db,
)
from juggle_db import DEFAULT_DATA_DIR as _DATA_DIR


def cmd_get_shared_context(args):
    db = get_db()
    rows = db.get_shared_context()

    if args.type:
        rows = [r for r in rows if r["context_type"] == args.type]
    if args.thread:
        rows = [r for r in rows if r.get("source_thread") == args.thread]
    if args.limit:
        rows = rows[-args.limit:]

    if args.plain:
        if not rows:
            print("(no shared context)")
            return
        for r in rows:
            src = f" (Thread {r['source_thread']})" if r.get("source_thread") else ""
            print(f"[{r['context_type']}]{src} {r['content']}")
    else:
        print(json.dumps(rows, indent=2))


def cmd_add_shared(args):
    db = get_db()
    db.add_shared(args.type, args.content, source_thread=args.thread)
    print(f"Added [{args.type}]: {args.content}")


def cmd_get_context(_):
    sys.path.insert(0, str(SRC_DIR))
    from juggle_context import build_context_string
    result = build_context_string(db_path=str(DB_PATH))
    print(result)


def cmd_init_db(_):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.init_db()
    print("DB initialized.")


def cmd_recall(args):
    """Recall memories from Hindsight for a thread."""
    client = _get_hindsight_client()
    if client is None:
        return  # disabled or unconfigured

    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    result = client.reflect(args.query)

    if result:
        db.update_thread(thread_uuid, memory_context=result, memory_loaded=1)
        print(result)
    else:
        db.update_thread(thread_uuid, memory_loaded=1)


def cmd_recall_if_cold(args):
    """Recall only if thread hasn't loaded memory yet."""
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    if thread.get("memory_loaded", 0):
        return  # already loaded, no-op

    client = _get_hindsight_client()
    if client is None:
        return

    result = client.reflect(args.query)
    if result:
        db.update_thread(thread_uuid, memory_context=result, memory_loaded=1)
        print(result)
    else:
        db.update_thread(thread_uuid, memory_loaded=1)


def cmd_retain(args):
    """Retain content as memory in Hindsight."""
    client = _get_hindsight_client()
    if client is None:
        return  # disabled or unconfigured

    context = getattr(args, "context", None)
    client.retain(args.content, context=context)


def cmd_grep_vault(args):
    """Search vault for terms. Returns matching file paths only."""
    vault = args.vault_path
    results = []
    for term in args.terms[:5]:
        try:
            proc = subprocess.run(
                ["grep", "-ril", "--include=*.md", term, vault],
                capture_output=True, text=True, timeout=5,
            )
            for line in proc.stdout.strip().split("\n"):
                if line and line not in results:
                    results.append(line)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    if results:
        print("\n".join(results[:20]))


def cmd_register_domain(args):
    db = get_db()
    db.register_domain(args.name)
    print(f"Domain '{args.name}' registered.")


def cmd_register_domain_path(args):
    db = get_db()
    if not db.is_known_domain(args.domain):
        print(f"Unknown domain '{args.domain}'. Run: juggle register-domain {args.domain}")
        sys.exit(1)
    db.add_domain_path(args.path_fragment, args.domain)
    print(f"Path '{args.path_fragment}' → domain '{args.domain}' registered.")


def _parse_cutoff(since: str) -> str:
    """Parse --since value to UTC ISO timestamp. Accepts 'today', 'yesterday', or ISO."""
    now_local = datetime.now().astimezone()
    if since == "today":
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif since == "yesterday":
        start = (now_local - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        try:
            start = datetime.fromisoformat(since)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Error: invalid --since value: {since}")
            sys.exit(1)
    return start.astimezone(timezone.utc).isoformat()


def _state_icon(status: str, agent_result: str | None) -> str:
    """Simple status icon for digest output."""
    if agent_result and agent_result.startswith("⚠️ BLOCKER:"):
        return "⚠️"
    return {
        "active": "👉", "background": "🏃", "done": "✅",
        "failed": "❌", "archived": "🗄️",
    }.get(status, "💤")


def cmd_digest(args):
    """Summarize topics, decisions, blockers, and agent activity since cutoff."""
    db = get_db()
    cutoff = _parse_cutoff(args.since)

    all_threads = db.get_all_threads()
    active_threads = [
        t for t in all_threads
        if t.get("status") != "archived"
        and ((t.get("last_active") or "") >= cutoff or (t.get("created_at") or "") >= cutoff)
    ]

    current_id = db.get_current_thread()
    lines: list[str] = []

    # Header
    lines.append("━" * 40)
    lines.append(f"  Juggle Digest  •  since {args.since}")
    lines.append("━" * 40)

    # TOPICS
    new_count = sum(1 for t in active_threads if (t.get("created_at") or "") >= cutoff)
    done_count = sum(1 for t in active_threads if t.get("status") == "done")
    running_count = sum(1 for t in active_threads if t.get("status") in ("active", "background"))
    lines.append(f"\n📦 TOPICS  ({running_count} active, {done_count} completed, {new_count} new)")

    for t in active_threads:
        label = t.get("label") or t["id"][:4]
        title = t.get("title") or t.get("topic") or ""
        status = t.get("status") or "active"
        icon = _state_icon(status, t.get("agent_result"))
        detail = _humanize_dt(t.get("last_active") or "")

        if (t.get("created_at") or "") >= cutoff and status == "active":
            tag = f"new {detail}"
        elif status == "done":
            tag = f"completed {detail}"
        elif status == "background":
            tag = "agent running"
        elif t["id"] == current_id:
            tag = "current"
        else:
            tag = f"idle {detail}"

        lines.append(f"  {icon} [{label}] {title}  — {tag}")

    # DECISIONS
    decisions: list[str] = []
    for t in active_threads:
        label = t.get("label") or t["id"][:4]
        kd = t.get("key_decisions") or "[]"
        if isinstance(kd, str):
            try:
                kd = json.loads(kd)
            except (json.JSONDecodeError, ValueError):
                kd = []
        for d in kd:
            decisions.append(f"  [{label}] • {d}")

    if decisions:
        lines.append(f"\n✅ DECISIONS  ({len(decisions)} total)")
        lines.extend(decisions)

    # BLOCKERS
    blockers: list[str] = []
    for t in all_threads:
        if t.get("status") == "archived":
            continue
        ar = t.get("agent_result") or ""
        if ar.startswith("⚠️ BLOCKER:"):
            label = t.get("label") or t["id"][:4]
            blockers.append(f"  [{label}] {ar[len('⚠️ BLOCKER:'):].strip()}")

    if blockers:
        lines.append("\n⚠️ BLOCKERS REMAINING")
        lines.extend(blockers)

    # AGENT ACTIVITY (fragile notification parse — v1)
    all_notifs = []
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT message, created_at FROM notifications WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        all_notifs = [dict(r) for r in rows]

    completed = sum(1 for n in all_notifs if "completed" in n["message"].lower())
    failed = sum(1 for n in all_notifs if "failed" in n["message"].lower())
    agents = db.get_all_agents()
    running = sum(1 for a in agents if a.get("status") == "busy")
    dispatched = completed + failed + running

    if dispatched or running:
        lines.append("\n🤖 AGENT ACTIVITY")
        lines.append(
            f"  {dispatched} dispatched  •  {completed} completed  •  "
            f"{running} running  •  {failed} failed"
        )

    # Hindsight reflect (optional, 30s timeout)
    try:
        client = _get_hindsight_client()
        if client:
            reflection = client.reflect("juggle activity summary", timeout=30)
            if reflection:
                lines.append("\n🧠 HINDSIGHT")
                lines.append(f"  {reflection}")
    except Exception:
        pass  # skip silently

    output = "\n".join(lines) + "\n"
    print(output)

    # --save
    if getattr(args, "save", False):
        log_dir = Path.home() / ".juggle" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        log_path = log_dir / f"juggle-digest-{date_str}.md"
        log_path.write_text(output)
        print(f"Saved to {log_path}")


def cmd_next_action(args):
    """Switch to the highest-priority action item (blocker > review > idle OQ > open question)."""
    db = get_db()
    current_id = db.get_current_thread()

    all_threads = db.get_all_threads()
    visible = [
        t for t in all_threads
        if t.get("show_in_list", 1) != 0 and t.get("status") != "archived"
    ]

    from juggle_db import _thread_age_seconds  # private import — acceptable for v1

    target_thread = None
    action_line = None

    # 1. Blocker
    for t in visible:
        ar = t.get("agent_result") or ""
        if ar.startswith("⚠️ BLOCKER:"):
            blocker_text = ar[len("⚠️ BLOCKER:"):].strip()
            label = t.get("label") or "?"
            target_thread = t
            action_line = f"⚠️ [{label}] BLOCKER: {blocker_text}"
            break

    # 2. Review: done + result + not current
    if target_thread is None:
        for t in visible:
            tid = t["id"]
            status = t.get("status") or "active"
            ar = t.get("agent_result") or ""
            if status == "done" and ar and tid != current_id:
                label = t.get("label") or "?"
                target_thread = t
                action_line = f"📬 [{label}] Agent finished — results ready"
                break

    # 3. Idle with open question (last_active > 2h)
    if target_thread is None:
        for t in visible:
            oq = json.loads(t.get("open_questions") or "[]")
            if not oq:
                continue
            age = _thread_age_seconds(t.get("last_active"))
            if age is not None and age > 2 * 3600:
                label = t.get("label") or "?"
                target_thread = t
                action_line = f"💬 [{label}] Idle with open questions"
                break

    # 4. Any thread with open questions
    if target_thread is None:
        for t in visible:
            oq = json.loads(t.get("open_questions") or "[]")
            if oq:
                label = t.get("label") or "?"
                target_thread = t
                action_line = f"❓ [{label}] {oq[0]}"
                break

    if target_thread is None:
        print("✓ No action items — all clear.")
        return

    print(action_line)

    # Switch to target thread unless already current
    if target_thread["id"] == current_id:
        return

    from juggle_cmd_threads import cmd_switch_thread
    import argparse
    switch_args = argparse.Namespace(thread_id=target_thread.get("label") or target_thread["id"])
    cmd_switch_thread(switch_args)
