#!/usr/bin/env python3
"""Juggle CLI — Thread lifecycle and display commands."""

import json
import sys
from pathlib import Path
import threading

from juggle_cli_common import (
    SRC_DIR,
    _generate_title_for_thread,
    _resolve_thread,
    get_db,
)
from juggle_context import get_thread_state
from juggle_db import DEFAULT_DATA_DIR as _DATA_DIR
from juggle_settings import get_settings as _get_settings


def _get_version():
    plugin_json = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(plugin_json.read_text())["version"]
    except Exception:
        return "?"


def _maybe_start_talkback() -> None:
    """Start talkback server if talkback.enabled=true and not already running."""
    import subprocess
    import urllib.request

    tb = _get_settings().get("talkback", {})
    if not tb.get("enabled", False):
        return

    port = int(tb.get("port", 18787))
    try:
        urllib.request.urlopen(f"http://localhost:{port}/health", timeout=0.5)
        return  # already running
    except Exception:
        pass

    plugin_root = Path(__file__).resolve().parent.parent
    talkback_bin = plugin_root / "scripts" / "talkback"
    if not talkback_bin.exists():
        return  # silently skip if not installed

    log_path = Path("/tmp/talkback.log")
    subprocess.Popen(
        [str(talkback_bin), "--listen", str(port)],
        stdout=log_path.open("a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def cmd_start(_):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    db = get_db()
    db.init_db()
    db.set_active(True)

    # Auto-start talkback if enabled in config
    _maybe_start_talkback()

    ver = _get_version()
    threads = db.get_all_threads()
    if not threads:
        thread_uuid = db.create_thread("General", session_id="")
        db.set_current_thread(thread_uuid)
        thread = db.get_thread(thread_uuid)
        label = (thread.get("user_label") or thread.get("label")) if thread else thread_uuid
        print(f"Juggle v{ver} started. Topic {label} created.")
    else:
        # Auto-switch to most recently active non-archived thread
        active_threads = [t for t in threads if t.get("status") != "archived"]
        if active_threads:
            most_recent = max(active_threads, key=lambda t: t.get("last_active") or "")
            db.set_current_thread(most_recent["id"])
        from juggle_context import build_startup_output
        print(build_startup_output(db))


def cmd_stop(_):
    db = get_db()
    db.set_active(False)

    threads = db.get_all_threads()
    if threads:
        print("Topics:")
        for t in threads:
            label = t.get("user_label") or t.get("label") or t["id"][:8]
            print(f"  [{label}] {t['topic']} — {t['status']}")
    else:
        print("No topics.")

    print("Juggle stopped.")


def cmd_create_thread(args):
    db = get_db()
    domain = getattr(args, "domain", None)
    if domain is not None and not db.is_known_domain(domain):
        print(f"Unknown domain '{domain}'. Run: juggle register-domain {domain}")
        sys.exit(1)
    thread_uuid = db.create_thread(args.topic, session_id="", domain=domain)
    db.set_current_thread(thread_uuid)
    thread = db.get_thread(thread_uuid)
    label = (thread.get("user_label") or thread.get("label")) if thread else thread_uuid
    domain_str = f" [domain={domain}]" if domain else ""
    print(f"Created Topic {label}: {args.topic}.{domain_str} Now in Topic {label}.")
    # Title generation is cosmetic — run in background so create-thread returns immediately.
    threading.Thread(
        target=_generate_title_for_thread,
        args=(get_db(), thread_uuid, args.topic),
        daemon=True,
    ).start()
    # Auto-recall: load memory context in background; joined so process doesn't exit first.
    def _auto_recall():
        from juggle_cli_common import _get_hindsight_client
        client = _get_hindsight_client()
        if client is None:
            return
        db2 = get_db()
        result = client.reflect(args.topic)
        if result:
            db2.update_thread(thread_uuid, memory_context=result, memory_loaded=1)
        else:
            db2.update_thread(thread_uuid, memory_loaded=1)
    t = threading.Thread(target=_auto_recall, daemon=False)
    t.start()
    t.join(timeout=10)


def _render_briefing(thread: dict, memories: list, db) -> str:
    """Render a structured context briefing for a thread. Template rendering — no LLM call."""
    from juggle_cli_common import _humanize_dt

    border = "═" * 44
    lines = []

    # ── Header ──────────────────────────────────────────────────────────────
    label = thread.get("user_label") or thread.get("label") or thread["id"][:8]
    title = thread.get("title") or thread.get("topic") or "Untitled"
    status = thread.get("status", "active")
    last_active_str = _humanize_dt(thread.get("last_active") or "")
    current_id = db.get_current_thread()
    state_emoji = get_thread_state(db, thread, current_id)

    lines.append(border)
    lines.append(f"  {state_emoji} [{label}] {title}")
    lines.append(f"  Status: {status}  •  Last active: {last_active_str}")
    lines.append(border)

    # ── Empty thread guard ───────────────────────────────────────────────────
    messages = db.get_messages(thread["id"], token_budget=4000)
    real_messages = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not real_messages:
        lines.append("")
        lines.append("New thread — no context yet.")
        lines.append("")
        return "\n".join(lines)

    # ── WHY ─────────────────────────────────────────────────────────────────
    summary = (thread.get("summary") or "").strip()
    lines.append("")
    lines.append("🎯 WHY")
    if summary:
        for para in summary.split("\n"):
            lines.append(f"  {para}")
    else:
        lines.append("  No summary yet.")
    # Blend up to 2 Hindsight memories
    for mem in memories[:2]:
        lines.append(f"  🧠 {mem}")

    # ── WHAT CHANGED ─────────────────────────────────────────────────────────
    lines.append("")
    lines.append("📝 WHAT CHANGED")
    asst_msgs = [m for m in real_messages if m.get("role") == "assistant"]
    if asst_msgs:
        last_asst = asst_msgs[-1].get("content", "").strip()
        if len(last_asst) > 300:
            last_asst = last_asst[:297] + "…"
        for para in last_asst.split("\n"):
            para = para.strip()
            if para:
                lines.append(f"  {para}")
    elif summary:
        for para in summary.split("\n"):
            lines.append(f"  {para}")
    else:
        lines.append("  —")

    # ── DECISIONS ────────────────────────────────────────────────────────────
    key_decisions_raw = thread.get("key_decisions") or "[]"
    try:
        key_decisions = json.loads(key_decisions_raw) if isinstance(key_decisions_raw, str) else (key_decisions_raw or [])
    except (json.JSONDecodeError, ValueError):
        key_decisions = []
    if key_decisions:
        lines.append("")
        lines.append("✅ DECISIONS  (and why)")
        for d in key_decisions:
            lines.append(f"  • {d}")

    # ── OPEN QUESTIONS ───────────────────────────────────────────────────────
    open_questions_raw = thread.get("open_questions") or "[]"
    try:
        open_questions = json.loads(open_questions_raw) if isinstance(open_questions_raw, str) else (open_questions_raw or [])
    except (json.JSONDecodeError, ValueError):
        open_questions = []
    if open_questions:
        lines.append("")
        lines.append("❓ OPEN QUESTIONS")
        for q in open_questions:
            lines.append(f"  • {q}")

    # ── NEXT STEPS ───────────────────────────────────────────────────────────
    lines.append("")
    lines.append("⚡ NEXT STEPS")
    agent_result = thread.get("agent_result") or ""
    if status == "background":
        lines.append("  • Agent is running — check back or work on another topic.")
    elif status == "failed":
        lines.append("  • Review what failed. Decide whether to retry or close.")
    elif agent_result.startswith("⚠️ BLOCKER:"):
        blocker_text = agent_result[len("⚠️ BLOCKER:"):].strip()
        lines.append(f"  • Resolve the blocker: {blocker_text}")
    elif status == "done" and open_questions:
        lines.append("  • Address open questions, then archive.")
    elif status == "done":
        lines.append("  • Archive this thread.")
    else:
        lines.append("  • Continue working.")

    lines.append(border)
    return "\n".join(lines)


def cmd_switch_thread(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.set_current_thread(thread_uuid)

    # Mark as reviewed if switching to a done thread with agent results
    if thread.get("status") == "done" and thread.get("agent_result"):
        db.update_thread(thread_uuid, reviewed=1)

    # Hardcoded recall — fires on every switch regardless of LLM behavior
    from juggle_context import _recall_for_thread
    memories = _recall_for_thread(thread["topic"])

    print(_render_briefing(thread, memories, db))


def cmd_update_meta(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    if args.add_decision:
        key_decisions = thread.get("key_decisions") or "[]"
        if isinstance(key_decisions, str):
            try:
                key_decisions = json.loads(key_decisions)
            except (json.JSONDecodeError, ValueError):
                key_decisions = []
        key_decisions.append(args.add_decision)
        db.update_thread(thread_uuid, key_decisions=key_decisions)

    if args.add_question:
        open_questions = thread.get("open_questions") or "[]"
        if isinstance(open_questions, str):
            try:
                open_questions = json.loads(open_questions)
            except (json.JSONDecodeError, ValueError):
                open_questions = []
        open_questions.append(args.add_question)
        db.update_thread(thread_uuid, open_questions=open_questions)

    if args.resolve_question:
        open_questions = thread.get("open_questions") or "[]"
        if isinstance(open_questions, str):
            try:
                open_questions = json.loads(open_questions)
            except (json.JSONDecodeError, ValueError):
                open_questions = []
        open_questions = [q for q in open_questions if q != args.resolve_question]
        db.update_thread(thread_uuid, open_questions=open_questions)

    label = thread.get("user_label") or thread.get("label") or args.thread_id
    print(f"Updated metadata for Thread {label}.")


def cmd_update_summary(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    if not db.get_thread(thread_uuid):
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    summary = args.summary
    # Truncate at word boundary if over configured max chars
    _max_chars = _get_settings()["summary_max_chars"]
    if len(summary) > _max_chars:
        summary = summary[:_max_chars].rsplit(' ', 1)[0]
    db.update_thread(thread_uuid, summary=summary)
    updated = db.get_thread(thread_uuid)
    label = (updated.get("user_label") or updated.get("label") if updated else None) or args.thread_id
    print(f"Summary updated for Thread {label}.")


def cmd_close_thread(args):
    import juggle_cli_common as _common
    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    label = thread.get("user_label") or thread.get("label") or args.thread_id
    db.set_thread_status(thread_uuid, "closed")
    print(f"Thread {label} ({thread['topic']}) closed.")


def _sort_key_for_topic(thread: dict, current_id: str, db) -> tuple:
    """Return a sort key tuple for cmd_show_topics ordering."""
    tid = thread["id"]
    emoji = get_thread_state(db, thread, current_id)

    if emoji == "⏸️":
        tier = 0
    elif emoji == "🏃\u200d♂️":
        tier = 1
    elif tid == current_id:
        tier = 2
    elif emoji in ("💤", "✅", "❌"):
        tier = 3
    else:
        tier = 2  # active but not current

    return (tier,)


def _cleanup_orphaned_threads(db) -> None:
    """Find 'running' threads with no busy agent; convert each to closed + action_item."""
    with db._connect() as conn:
        orphans = conn.execute(
            """
            SELECT t.id, t.user_label, t.label, t.topic FROM threads t
            WHERE t.status = 'running'
            AND NOT EXISTS (
                SELECT 1 FROM agents a WHERE a.assigned_thread = t.id AND a.status = 'busy'
            )
            """
        ).fetchall()
    for o in orphans:
        db.add_action_item(
            thread_id=o["id"],
            message=f"orphaned running thread ({o['topic']}) — no busy agent. Review.",
            type_="failure",
            priority="high",
        )
        db.set_thread_status(o["id"], "closed")


def cmd_show_topics(_):
    from juggle_context import render_topics_tree
    db = get_db()
    _cleanup_orphaned_threads(db)
    threads = db.get_all_threads()
    if not threads:
        print("No topics.")
        return
    print(render_topics_tree(db))


def cmd_get_archive_candidates(_):
    db = get_db()
    candidates = db.get_archive_candidates()
    if not candidates:
        print("No archive candidates.")
        return
    for t in candidates:
        label = t.get("user_label") or t.get("label") or t["id"][:8]
        title = t.get("title") or t["topic"]
        status = t["status"]
        last_active = t.get("last_active") or ""
        print(f"[{label}] {title}  {status}  ({last_active})")


def cmd_archive_thread(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    label = thread.get("user_label") or thread.get("label") or args.thread_id
    db.archive_thread(thread_uuid)

    # Decommission agents still assigned to this thread
    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager
    mgr = JuggleTmuxManager()
    assigned = [a for a in db.get_all_agents() if a.get("assigned_thread") == thread_uuid]
    for agent in assigned:
        status = agent["status"]
        if status == "idle":
            mgr.decommission_agent(db, agent["id"])
        elif status == "busy":
            db.update_agent(agent["id"], status="decommission_pending")

    print(f"Thread {label} archived.")


def cmd_unarchive_thread(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    db.unarchive_thread(thread_uuid)
    thread = db.get_thread(thread_uuid)
    label = (thread.get("user_label") or thread.get("label")) if thread else thread_uuid
    print(f"Thread {label} unarchived.")


def cmd_generate_title(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    title = _generate_title_for_thread(db, thread_uuid, thread["topic"])
    print(title)


def cmd_backfill_titles(_):
    db = get_db()
    threads = db.get_all_threads()
    missing = [t for t in threads if not t.get("title")]
    if not missing:
        print("All threads have titles.")
        return
    for t in missing:
        label = t.get("user_label") or t.get("label") or t["id"][:8]
        print(f"Generating title for thread {label}...")
        _generate_title_for_thread(db, t["id"], t["topic"])
    print(f"Backfilled {len(missing)} threads.")


def cmd_set_summarized_count(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    if not db.get_thread(thread_uuid):
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    db.update_thread(thread_uuid, summarized_msg_count=args.count)
    updated = db.get_thread(thread_uuid)
    label = (updated.get("user_label") or updated.get("label") if updated else None) or args.thread_id
    print(f"Summarized count set to {args.count} for Thread {label}.")


def cmd_get_stale_threads(args):
    db = get_db()
    stale = db.get_stale_threads(threshold=args.threshold)
    if not stale:
        print("No stale threads.")
        return
    for t in stale:
        label = t.get("user_label") or t.get("label") or t["id"][:8]
        print(f"{label} {t['topic']} (delta={t['delta']})")


def cmd_get_messages(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    messages = db.get_messages(thread_uuid, token_budget=9999)
    if args.limit:
        messages = messages[-args.limit:]
    if not messages:
        print("No messages.")
        return
    if args.plain:
        for m in messages:
            print(f"{m['role']}: {m['content']}")
    else:
        for m in messages:
            content = m["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"  [{m['role']}] {content}")
