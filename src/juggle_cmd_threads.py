#!/usr/bin/env python3
"""Juggle CLI — Thread lifecycle and display commands."""

import json
import logging
import os
import sys
from pathlib import Path
import threading

_log = logging.getLogger(__name__)
_Path = Path

from juggle_cmd_projects import assign_project_background
from juggle_cli_common import (
    SRC_DIR,
    _generate_title_for_thread,
    _resolve_thread,
    get_db,
)
from juggle_context import get_thread_state
from juggle_db import DEFAULT_DATA_DIR as _DATA_DIR
from juggle_settings import get_settings as _get_settings
from dbops.schema import is_auto_topic_eligible


def _get_version():
    plugin_json = (
        Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    )
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


def _start_watchdog_for_cmd_start(db) -> None:
    """`juggle start` is the CLI start/unfreeze path for the watchdog.

    Clears any freeze sentinel set by ``stop-watchdog --freeze`` (otherwise a CLI
    user who froze is stranded — only the cockpit W/R hotkeys cleared it) and
    ensures the singleton watchdog is up. Reuses the flock-based singleton
    primitives (the cockpit's ensure path); ``force=True`` because an explicit
    start must bypass the respawn debounce. Fail-silent: a watchdog hiccup must
    never block session activation.
    """
    try:
        import juggle_watchdog_singleton as ws

        db_path = str(db.db_path)
        ws.unfreeze_watchdog(db_path)
        ws.ensure_watchdog(
            db_path, repo_path=ws.canonical_repo_path(), force=True
        )
    except Exception:
        _log.warning("cmd_start: watchdog start/unfreeze failed", exc_info=True)


def cmd_start(_):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    db = get_db()
    db.init_db()
    db.set_active(True)
    # Record which Claude Code session is the orchestrator so PreToolUse can
    # scope the edit-block to exactly this session, not every active session.
    orch_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    db.set_orchestrator_session_id(orch_sid)

    # Auto-start talkback if enabled in config
    _maybe_start_talkback()
    # `juggle start` is the CLI start/unfreeze path: clear any freeze sentinel and
    # ensure the singleton watchdog is alive (the cockpit also self-heals it on an
    # interval, but a headless CLI user needs this to escape a freeze).
    _start_watchdog_for_cmd_start(db)

    ver = _get_version()
    threads = db.get_all_threads()
    if not threads:
        thread_uuid = db.create_thread("General", session_id="")
        db.set_current_thread(thread_uuid)
        thread = db.get_thread(thread_uuid)
        label = (
            (thread.get("user_label") or thread.get("label")) if thread else thread_uuid
        )
        print(f"Juggle v{ver} started. Topic {label} created.")
    else:
        # Auto-switch to most recently active non-archived thread
        active_threads = [t for t in threads if t.get("state") != "archived"]
        if active_threads:
            most_recent = max(active_threads, key=lambda t: t.get("last_active_at") or "")
            db.set_current_thread(most_recent["id"])
        from juggle_context import build_startup_output

        print(build_startup_output(db))


def cmd_stop(_):
    db = get_db()
    db.set_active(False)
    db.set_orchestrator_session_id("")  # clear so stale sessions don't haunt new ones

    threads = db.get_all_threads()
    if threads:
        print("Topics:")
        for t in threads:
            label = t.get("user_label") or t.get("label") or t["id"][:8]
            print(f"  [{label}] {t['title']} — {t['state']}")
    else:
        print("No topics.")

    # Stop the watchdog via the flock-based singleton — the ONE coordination
    # primitive (the per-DB lock IS singleton truth). Fail-silent so a watchdog
    # hiccup never blocks session deactivation.
    try:
        from juggle_watchdog_singleton import stop_watchdog
        stop_watchdog(str(db.db_path))
    except Exception:
        _log.warning("cmd_stop: watchdog stop failed", exc_info=True)
    print("Juggle stopped.")


def _create_node_for_thread(db, thread_id: str, topic: str) -> None:
    """P5 shim helper: write a nodes row mirroring an existing threads row.

    Called AFTER db.create_thread() so the thread row already exists.
    INSERT OR IGNORE is safe to call multiple times (idempotent).
    Fail-silent: if the nodes table doesn't exist yet or db is mocked, skip.
    """
    try:
        from juggle_add_node import _create_node_row
        _create_node_row(
            db, node_id=thread_id, kind="conversation", title=topic,
            objective="", state="open", project_id=None,
            verify_cmd=None, parent_id=None,
        )
    except Exception:
        pass


def cmd_create_thread(args):
    if not is_auto_topic_eligible(args.topic):
        print(
            f"[juggle] Skipped: topic looks like orchestrator chatter and will not create a thread.",
            file=sys.stderr,
        )
        sys.exit(1)
    db = get_db()
    thread_uuid = db.create_thread(args.topic, session_id="")
    # P5 shim: also write a unified nodes row for this conversation.
    _create_node_for_thread(db, thread_uuid, args.topic)
    db.set_current_thread(thread_uuid)
    thread = db.get_thread(thread_uuid)
    label = (thread.get("user_label") or thread.get("label")) if thread else thread_uuid
    print(f"Created Topic {label}: {args.topic}. Now in Topic {label}.")
    # Title generation is cosmetic — run in background so create-thread returns immediately.
    threading.Thread(
        target=_generate_title_for_thread,
        args=(get_db(), thread_uuid, args.topic),
        daemon=True,
    ).start()
    # Project assignment — async, fail-silent, never blocks
    assign_project_background(get_db(), thread_uuid, args.topic)


def close_junk_threads(db) -> list[dict]:
    """Close auto-created junk threads that have no real messages and no worktree.

    A thread is junk when its original topic contains an orchestrator-chatter
    marker (AUTOPILOT MODE, JUGGLE ACTIVE, loop-tick headers, etc.) AND it has
    no real user messages and no worktree attached.  Returns list of closed thread
    dicts so callers can report what was cleaned up.
    """
    closed: list[dict] = []
    # Junk detection keys on the IMMUTABLE original topic (the orchestrator-chatter
    # marker). The conversation node only carries the (regeneratable) title, so the
    # original topic is read from the legacy threads row — the write path still
    # populates it (P8 conversation read-collapse keeps threads writes for now).
    with db._connect() as conn:
        orig_topics = {
            r["id"]: r["topic"]
            for r in conn.execute("SELECT id, topic FROM threads").fetchall()
        }
    for thread in db.get_all_threads():
        if thread.get("state") in ("archived", "done"):
            continue
        topic = orig_topics.get(thread["id"]) or thread.get("title") or ""
        if is_auto_topic_eligible(topic):
            continue  # not chatter — leave it alone
        if thread.get("worktree_path"):
            continue  # real work was done
        if db.get_message_count(thread["id"], exclude_junk=True) > 0:
            continue  # real messages exist
        db.set_thread_status(thread["id"], "closed")
        closed.append(thread)
    return closed



def _render_briefing(thread: dict, db) -> str:
    """Render a structured context briefing for a thread. Template rendering — no LLM call."""
    from juggle_cli_common import _humanize_dt

    border = "═" * 44
    lines = []

    # ── Header ──────────────────────────────────────────────────────────────
    label = thread.get("user_label") or thread.get("label") or thread["id"][:8]
    title = thread.get("title") or "Untitled"
    state = thread.get("state", "open")
    last_active_str = _humanize_dt(thread.get("last_active_at") or "")
    current_id = db.get_current_thread()
    state_emoji = get_thread_state(db, thread, current_id)

    lines.append(border)
    lines.append(f"  {state_emoji} [{label}] {title}")
    lines.append(f"  Status: {state}  •  Last active: {last_active_str}")
    lines.append(border)

    # ── Empty thread guard ───────────────────────────────────────────────────
    messages = db.get_messages(thread["id"], token_budget=4000)
    real_messages = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not real_messages:
        lines.append("")
        lines.append("New thread — no context yet.")
        lines.append("")
        return "\n".join(lines)

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
        key_decisions = (
            json.loads(key_decisions_raw)
            if isinstance(key_decisions_raw, str)
            else (key_decisions_raw or [])
        )
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
        open_questions = (
            json.loads(open_questions_raw)
            if isinstance(open_questions_raw, str)
            else (open_questions_raw or [])
        )
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
        blocker_text = agent_result[len("⚠️ BLOCKER:") :].strip()
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
    if thread.get("state") == "done" and thread.get("agent_result"):
        db.update_thread(thread_uuid, reviewed=1)

    print(_render_briefing(thread, db))


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
    dismissed = db.dismiss_action_items_for_thread(thread_uuid)
    suffix = (
        f" ({dismissed} action item{'s' if dismissed != 1 else ''} closed)"
        if dismissed
        else ""
    )
    print(f"Thread {label} ({thread['title']}) closed.{suffix}")


def _cleanup_orphaned_threads(db) -> None:
    """Find 'running' threads with no busy agent; convert each to closed + action_item."""
    with db._connect() as conn:
        # P8 Task 3.1: conversations read from nodes (kind='conversation');
        # status='running'->state='running', topic->title.
        orphans = conn.execute(
            """
            SELECT t.id, t.user_label, t.title FROM nodes t
            WHERE t.kind = 'conversation' AND t.state = 'running'
            AND NOT EXISTS (
                SELECT 1 FROM agents a WHERE a.assigned_thread = t.id AND a.status = 'busy'
            )
            """
        ).fetchall()
    for o in orphans:
        db.add_action_item(
            thread_id=o["id"],
            message=f"orphaned running thread ({o['title']}) — no busy agent. Review.",
            type_="failure",
            priority="high",
        )
        db.set_thread_status(o["id"], "closed")


def cmd_show_topics(_):
    from datetime import datetime, timezone
    from juggle_cli_common import _extract_decision_prompt

    db = get_db()
    _cleanup_orphaned_threads(db)
    threads = [t for t in db.get_all_threads() if t.get("state") != "archived"]
    if not threads:
        print("No topics.")
        return
    now = datetime.now(timezone.utc)
    for t in threads:
        lbl = t.get("user_label") or t["id"][:6]
        status = t.get("state") or "open"
        title = (t.get("title") or "")[:30]
        last_active = t.get("last_active_at") or ""
        age = "-"
        if last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                secs = int((now - dt).total_seconds())
                age = (
                    f"{secs}s"
                    if secs < 60
                    else (f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h")
                )
            except (ValueError, TypeError):
                pass
        line = f"[{lbl}] {status:<8} {title:<30} {age}"
        # Detect waiting thread (last assistant message ends with "?")
        with db._connect() as conn:
            asst = conn.execute(
                "SELECT content FROM messages WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (t["id"],),
            ).fetchone()
            usr = conn.execute(
                "SELECT content FROM messages WHERE thread_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
                (t["id"],),
            ).fetchone()
        if asst and asst["content"].rstrip().endswith("?"):
            prompt = _extract_decision_prompt(
                asst["content"], usr["content"] if usr else None
            )
            line += f"  {prompt}"
        print(line)


def cmd_get_archive_candidates(_):
    db = get_db()
    candidates = db.get_archive_candidates()
    if not candidates:
        print("No archive candidates.")
        return
    for t in candidates:
        label = t.get("user_label") or t.get("label") or t["id"][:8]
        title = t.get("title") or ""
        status = t["state"]
        last_active = t.get("last_active_at") or ""
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
    # P8 (Task 4.2): no graph_topics mirror to prune — archive_thread already takes
    # the conversation node terminal (kind='conversation', state mirrored).

    # Decommission agents still assigned to this thread
    sys.path.insert(0, str(SRC_DIR))
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager()
    assigned = [
        a for a in db.get_all_agents() if a.get("assigned_thread") == thread_uuid
    ]
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


def cmd_set_summarized_count(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    if not db.get_thread(thread_uuid):
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    db.update_thread(thread_uuid, summarized_msg_count=args.count)
    updated = db.get_thread(thread_uuid)
    label = (
        updated.get("user_label") or updated.get("label") if updated else None
    ) or args.thread_id
    print(f"Summarized count set to {args.count} for Thread {label}.")


def cmd_get_stale_threads(args):
    db = get_db()
    stale = db.get_stale_threads(threshold=args.threshold)
    if not stale:
        print("No stale threads.")
        return
    for t in stale:
        label = t.get("user_label") or t.get("label") or t["id"][:8]
        print(f"{label} {t['title']} (delta={t['delta']})")


def cmd_get_messages(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    messages = db.get_messages(thread_uuid, token_budget=9999)
    if args.limit:
        messages = messages[-args.limit :]
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
