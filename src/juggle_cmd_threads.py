#!/usr/bin/env python3
"""Juggle CLI — Thread lifecycle and display commands."""

import json
import sys
import threading

from juggle_cli_common import (
    SRC_DIR,
    _extract_decision_prompt,
    _generate_title_for_thread,
    _get_hindsight_client,
    _humanize_dt,
    _last_sentences,
    _resolve_thread,
    get_db,
)
from juggle_context import get_thread_state
from juggle_db import DEFAULT_DATA_DIR as _DATA_DIR


def cmd_start(_):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    db = get_db()
    db.init_db()
    db.set_active(True)

    threads = db.get_all_threads()
    if not threads:
        thread_uuid = db.create_thread("General", session_id="")
        db.set_current_thread(thread_uuid)
        thread = db.get_thread(thread_uuid)
        label = thread["label"] if thread else thread_uuid
        print(f"Juggle started. Topic {label} created. Use 'create-thread <topic>' to create more topics.")
    else:
        current = db.get_current_thread()
        if not current and threads:
            db.set_current_thread(threads[0]["id"])
        print("Juggle started.")


def cmd_stop(_):
    db = get_db()
    db.set_active(False)

    threads = db.get_all_threads()
    if threads:
        print("Topics:")
        for t in threads:
            label = t.get("label") or t["id"][:8]
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
    label = thread["label"] if thread else thread_uuid
    domain_str = f" [domain={domain}]" if domain else ""
    print(f"Created Topic {label}: {args.topic}.{domain_str} Now in Topic {label}.")
    # Title generation is cosmetic — run in background so create-thread returns immediately.
    threading.Thread(
        target=_generate_title_for_thread,
        args=(get_db(), thread_uuid, args.topic),
        daemon=True,
    ).start()
    # Auto-recall memory for the new thread.
    def _auto_recall():
        try:
            client = _get_hindsight_client()
            if client is None:
                return
            result = client.recall(args.topic)
            db2 = get_db()
            if result:
                db2.update_thread(thread_uuid, memory_context=result, memory_loaded=1)
            else:
                db2.update_thread(thread_uuid, memory_loaded=1)
        except Exception:
            pass  # non-blocking

    recall_thread = threading.Thread(target=_auto_recall, daemon=False)
    recall_thread.start()
    recall_thread.join(timeout=10)


def cmd_switch_thread(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.set_current_thread(thread_uuid)
    label = thread.get("label") or thread_uuid[:8]

    print(f"=== Topic {label}: {thread['topic']} ===")
    print(f"Status: {thread['status']}")

    if thread.get("summary"):
        print(f"\nSummary:\n{thread['summary']}")

    key_decisions = thread.get("key_decisions") or "[]"
    if isinstance(key_decisions, str):
        try:
            key_decisions = json.loads(key_decisions)
        except (json.JSONDecodeError, ValueError):
            key_decisions = []
    if key_decisions:
        print("\nKey Decisions:")
        for d in key_decisions:
            print(f"  - {d}")

    open_questions = thread.get("open_questions") or "[]"
    if isinstance(open_questions, str):
        try:
            open_questions = json.loads(open_questions)
        except (json.JSONDecodeError, ValueError):
            open_questions = []
    if open_questions:
        print("\nOpen Questions:")
        for q in open_questions:
            print(f"  ? {q}")

    messages = db.get_messages(thread_uuid, token_budget=2000)
    if messages:
        print("\nRecent messages:")
        for m in messages[-5:]:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"  [{role}] {content}")


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

    label = thread.get("label") or args.thread_id
    print(f"Updated metadata for Thread {label}.")


def cmd_update_summary(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    if not db.get_thread(thread_uuid):
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    summary = args.summary
    # Truncate at word boundary if over 250 chars
    if len(summary) > 250:
        summary = summary[:250].rsplit(' ', 1)[0]
    db.update_thread(thread_uuid, summary=summary)
    updated = db.get_thread(thread_uuid)
    label = (updated.get("label") if updated else None) or args.thread_id
    print(f"Summary updated for Thread {label}.")


def cmd_close_thread(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    label = thread.get("label") or args.thread_id
    db.update_thread(thread_uuid, status="done")
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
    """Mark background threads with no busy agent as failed."""
    with db._connect() as conn:
        orphans = conn.execute(
            """
            SELECT t.id, t.label FROM threads t
            WHERE t.status = 'background'
            AND NOT EXISTS (
                SELECT 1 FROM agents a WHERE a.assigned_thread = t.id AND a.status = 'busy'
            )
            """
        ).fetchall()
        for t in orphans:
            label = t["label"] or t["id"][:8]
            conn.execute(
                "UPDATE threads SET status = 'failed' WHERE id = ?",
                (t["id"],),
            )
        conn.commit()
    for t in orphans:
        label = t["label"] or t["id"][:8]
        db.add_notification(t["id"],
            f"[Topic {label} failed] No agent assigned — orphaned thread cleaned up.")


def cmd_show_topics(_):
    db = get_db()

    # Clean up orphaned background threads before rendering
    _cleanup_orphaned_threads(db)

    threads = db.get_all_threads()
    if not threads:
        print("No topics.")
        return

    current = db.get_current_thread()

    # Filter out archived threads (show_in_list = 0)
    threads = [t for t in threads if t.get("show_in_list", 1) != 0]

    if not threads:
        print("No topics.")
        return

    def _full_sort_key(t: dict) -> tuple:
        tier = _sort_key_for_topic(t, current or "", db)[0]
        last_active = t.get("last_active") or ""
        inverted = "".join(chr(0x10FFFF - ord(c)) for c in last_active) if last_active else ""
        return (tier, inverted)

    threads.sort(key=_full_sort_key)

    _state_suffix_text = {
        "👉": "← YOU ARE HERE",
        "🏃\u200d♂️": "agent running",
        "⏸️": "waiting for you",
        "💤": "idle",
        "✅": "done",
        "❌": "failed",
        "🗄️": "archived",
        "": "",
    }

    print("Topics")
    last_idx = len(threads) - 1
    for idx, t in enumerate(threads):
        is_last = idx == last_idx
        branch = "└──" if is_last else "├──"
        vert = "    " if is_last else "│   "

        tid = t["id"]
        label = t.get("label") or tid[:8]
        topic = t["topic"]
        title = t.get("title") or topic
        last_active = _humanize_dt(t.get("last_active") or "")

        emoji = get_thread_state(db, t, current or "")
        state_suffix = _state_suffix_text.get(emoji, "")

        header = f"{branch} {emoji} **[{label}] {title}**  ({last_active})"
        if state_suffix:
            header = f"{header}  {state_suffix}"
        print(header)

        summary = (t.get("summary") or "").strip()
        summary_text = summary if summary else "no summary yet"
        print(f"{vert}├── Summary: {summary_text}")

        key_decisions_raw = t.get("key_decisions") or "[]"
        if isinstance(key_decisions_raw, str):
            try:
                key_decisions = json.loads(key_decisions_raw)
            except (json.JSONDecodeError, ValueError):
                key_decisions = []
        else:
            key_decisions = key_decisions_raw
        for decision in key_decisions:
            print(f"{vert}├── ✅ {decision}")

        open_questions_raw = t.get("open_questions") or "[]"
        if isinstance(open_questions_raw, str):
            try:
                open_questions = json.loads(open_questions_raw)
            except (json.JSONDecodeError, ValueError):
                open_questions = []
        else:
            open_questions = open_questions_raw
        for question in open_questions:
            print(f"{vert}├── ❓ {question}")

        status = t["status"]
        if status == "background":
            agent_status = t.get("last_user_intent") or t.get("agent_task_id") or "running..."
            print(f"{vert}├── ⏳ {agent_status}")

        if emoji == "⏸️":
            exchange = db.get_last_exchange(tid)
            decision = _extract_decision_prompt(
                exchange.get("last_assistant"),
                exchange.get("last_user"),
            )
            print(f"{vert}└── {decision}")
        else:
            exchanges = db.get_recent_exchanges(tid, n=2)
            exchange_labels = ["Last:", "Prior:"]
            for ex_idx, exchange in enumerate(exchanges):
                ex_label = exchange_labels[ex_idx] if ex_idx < len(exchange_labels) else "     "
                user_text = _last_sentences(exchange.get("user") or "")
                asst_text = _last_sentences(exchange.get("assistant") or "")
                user_display = f'"{user_text}"' if user_text else "(none)"
                asst_display = f'"{asst_text}"' if asst_text else "(none)"
                is_last_exchange = ex_idx == len(exchanges) - 1 and True
                connector = "└──" if is_last_exchange else "├──"
                print(f"{vert}{connector} {ex_label} Q: {user_display}")
                print(f"{vert}         A: {asst_display}")

        if not is_last:
            print("│")

    print()
    print('Use "/juggle:resume-topic <id>" to switch topics, or just keep talking.')


def cmd_get_archive_candidates(_):
    db = get_db()
    candidates = db.get_archive_candidates()
    if not candidates:
        print("No archive candidates.")
        return
    for t in candidates:
        label = t.get("label") or t["id"][:8]
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
    label = thread.get("label") or args.thread_id
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
    label = db.unarchive_thread(thread_uuid)
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
        label = t.get("label") or t["id"][:8]
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
    label = (updated.get("label") if updated else None) or args.thread_id
    print(f"Summarized count set to {args.count} for Thread {label}.")


def cmd_get_stale_threads(args):
    db = get_db()
    stale = db.get_stale_threads(threshold=args.threshold)
    if not stale:
        print("No stale threads.")
        return
    for t in stale:
        label = t.get("label") or t["id"][:8]
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
