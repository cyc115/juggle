#!/usr/bin/env python3
"""
Juggle CLI - called by LLM via Bash tool for state changes.
Usage: python juggle_cli.py <command> [args]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(os.environ.get("CLAUDE_PLUGIN_DATA", Path.home() / ".claude" / "juggle"))
DB_PATH = Path(os.environ["_JUGGLE_TEST_DB"]) if "_JUGGLE_TEST_DB" in os.environ else _DATA_DIR / "juggle.db"
SRC_DIR = Path(__file__).parent


def get_db():
    sys.path.insert(0, str(SRC_DIR))
    from juggle_db import JuggleDB
    return JuggleDB(str(DB_PATH))


def _resolve_thread(db, label_or_id: str) -> str:
    """Accept label (e.g. 'A') or UUID. Return UUID.

    Raises SystemExit(1) if the label is not found.
    """
    if len(label_or_id) == 1 and label_or_id.isalpha():
        thread = db.get_thread_by_label(label_or_id.upper())
        if not thread:
            print(f"Error: No active thread with label '{label_or_id.upper()}'.")
            sys.exit(1)
        return thread["id"]
    return label_or_id  # already a UUID


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
    thread_uuid = db.create_thread(args.topic, session_id="")
    db.set_current_thread(thread_uuid)
    thread = db.get_thread(thread_uuid)
    label = thread["label"] if thread else thread_uuid
    print(f"Created Topic {label}: {args.topic}. Now in Topic {label}.")


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

    if args.intent:
        db.update_thread(thread_uuid, last_user_intent=args.intent)

    label = thread.get("label") or args.thread_id
    print(f"Updated metadata for Thread {label}.")


def cmd_update_summary(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    if not db.get_thread(thread_uuid):
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    db.update_thread(thread_uuid, summary=args.summary)
    label = db.get_thread(thread_uuid).get("label") or args.thread_id
    print(f"Summary updated for Thread {label}.")


def cmd_close_thread(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    label = thread.get("label") or args.thread_id
    db.update_thread(thread_uuid, status="closed")
    print(f"Thread {label} ({thread['topic']}) closed.")


def _humanize_dt(iso_str: str) -> str:
    """Return a human-friendly relative time string for an ISO-8601 UTC timestamp."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = (now - dt).total_seconds()
        if diff < 60:
            return "just now"
        if diff < 3600:
            mins = int(diff // 60)
            return f"{mins} min ago"
        if diff < 86400:
            hrs = int(diff // 3600)
            return f"{hrs} hr ago"
        if diff < 172800:
            return "yesterday"
        days = int(diff // 86400)
        if days < 7:
            return f"{days} days ago"
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_str


def _last_sentences(text: str, max_chars: int = 200) -> str:
    """Return the last 1-2 sentences of text, capped at max_chars."""
    if not text:
        return ""
    text = text.strip()
    # Split on sentence-ending punctuation followed by whitespace
    import re
    parts = re.split(r"(?<=[.?!])\s+", text)
    parts = [p for p in parts if p]
    if not parts:
        return text[:max_chars]
    # Take last 2 non-empty parts
    snippet = " ".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    if len(snippet) > max_chars:
        snippet = snippet[-max_chars:].lstrip()
    return snippet


def _sort_key_for_topic(thread: dict, current_id: str, db) -> tuple:
    """Return a sort key tuple for cmd_show_topics ordering.

    Order: Waiting (⏸️) → Agent running (🏃) → Active/current → Idle/done → Archived
    Lower tuple value = shown first.
    """
    tid = thread["id"]
    emoji = db.get_thread_state(thread, current_id)

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


def cmd_show_topics(_):
    db = get_db()
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

    # Sort: Waiting → Agent running → Active/current → Idle/done → Archived
    # Within each tier, most-recently-active first (ISO timestamps sort lexicographically;
    # invert chars so descending order is achieved with a plain ascending sort).
    def _full_sort_key(t: dict) -> tuple:
        tier = _sort_key_for_topic(t, current or "", db)[0]
        last_active = t.get("last_active") or ""
        inverted = "".join(chr(0x10FFFF - ord(c)) for c in last_active) if last_active else ""
        return (tier, inverted)

    threads.sort(key=_full_sort_key)

    # State suffix text map
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
        status = t["status"]
        last_active = _humanize_dt(t.get("last_active") or "")

        # State emoji and suffix
        emoji = db.get_thread_state(t, current or "")
        state_suffix = _state_suffix_text.get(emoji, "")

        # Header line
        header = f"{branch} {emoji} **[{label}] {topic}**  ({last_active})"
        if state_suffix:
            header = f"{header}  {state_suffix}"
        print(header)

        # Summary (always shown)
        summary = (t.get("summary") or "").strip()
        summary_text = summary if summary else "no summary yet"
        print(f"{vert}├── Summary: {summary_text}")

        # Key decisions
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

        # Open questions
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

        # For waiting threads: show full pending question (the last assistant message)
        if emoji == "⏸️":
            with __import__('sqlite3').connect(str(db.db_path)) as conn:
                conn.row_factory = __import__('sqlite3').Row
                row = conn.execute(
                    """
                    SELECT content FROM messages
                    WHERE thread_id = ? AND role = 'assistant'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (tid,),
                ).fetchone()
            if row:
                print(f"{vert}├── ❓ {row['content']}")

        # For background threads: show agent_status line
        if status == "background":
            agent_status = t.get("last_user_intent") or t.get("agent_task_id") or "running..."
            print(f"{vert}├── ⏳ {agent_status}")

        # Last 2 exchanges
        exchanges = db.get_recent_exchanges(tid, n=2)
        exchange_labels = ["Last:", "Prior:"]
        for ex_idx, exchange in enumerate(exchanges):
            label = exchange_labels[ex_idx] if ex_idx < len(exchange_labels) else "     "
            user_text = _last_sentences(exchange.get("user") or "")
            asst_text = _last_sentences(exchange.get("assistant") or "")
            user_display = f'"{user_text}"' if user_text else "(none)"
            asst_display = f'"{asst_text}"' if asst_text else "(none)"
            is_last_exchange = ex_idx == len(exchanges) - 1 and True
            connector = "└──" if is_last_exchange else "├──"
            print(f"{vert}{connector} {label} Q: {user_display}")
            print(f"{vert}         A: {asst_display}")

        # Blank separator between threads (but not after the last one)
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
        topic = t["topic"]
        status = t["status"]
        last_active = t.get("last_active") or ""
        print(f"[{label}] {topic}  {status}  ({last_active})")


def cmd_archive_thread(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    label = thread.get("label") or args.thread_id if thread else args.thread_id
    db.archive_thread(thread_uuid)
    print(f"Thread {label} archived.")


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

    db.update_thread(thread_uuid, agent_result=args.result_summary, status="done")

    # Store the agent result as an assistant message so it's visible in get_last_exchange.
    if args.result_summary:
        db.add_message(thread_uuid, role="assistant", content=args.result_summary)

    # Auto-generate summary if the thread has none yet
    if not (thread.get("summary") or "").strip():
        exchange = db.get_last_exchange(thread_uuid)
        raw_last_user = exchange.get("last_user") or ""
        # Skip auto-summary if the last user message looks like junk (task notifications,
        # slash commands, or internal plumbing rather than real conversation).
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
                db.update_thread_summary(thread_uuid, auto_summary)

    notification = (
        f"[Topic {label} completed] {thread['topic']} — results ready. "
        f"Use: python juggle_cli.py switch-thread {label}"
    )
    db.add_notification(thread_uuid, notification)
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


def cmd_get_context(_):
    sys.path.insert(0, str(SRC_DIR))
    from juggle_context import build_context_string
    result = build_context_string(db_path=str(DB_PATH))
    print(result)


def cmd_set_summarized_count(args):
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    if not db.get_thread(thread_uuid):
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    db.set_summarized_count(thread_uuid, args.count)
    label = db.get_thread(thread_uuid).get("label") or args.thread_id
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


def cmd_init_db(_):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.init_db()
    print("DB initialized.")


def main():
    parser = argparse.ArgumentParser(
        description="Juggle CLI - multi-topic conversation orchestrator"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = subparsers.add_parser("start", help="Start juggle mode")
    p_start.add_argument("--session-id", dest="session_id", default=None)
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop juggle mode")
    p_stop.set_defaults(func=cmd_stop)

    # create-thread
    p_create = subparsers.add_parser("create-thread", help="Create a new topic thread")
    p_create.add_argument("topic", help="Topic name")
    p_create.set_defaults(func=cmd_create_thread)

    # switch-thread
    p_switch = subparsers.add_parser("switch-thread", help="Switch to a topic thread")
    p_switch.add_argument("thread_id", help="Thread ID (e.g. A, B, C)")
    p_switch.set_defaults(func=cmd_switch_thread)

    # update-meta
    p_meta = subparsers.add_parser("update-meta", help="Update thread metadata")
    p_meta.add_argument("thread_id", help="Thread ID")
    p_meta.add_argument("--add-decision", dest="add_decision", default=None, metavar="TEXT")
    p_meta.add_argument("--add-question", dest="add_question", default=None, metavar="TEXT")
    p_meta.add_argument("--resolve-question", dest="resolve_question", default=None, metavar="TEXT")
    p_meta.add_argument("--intent", dest="intent", default=None, metavar="TEXT")
    p_meta.set_defaults(func=cmd_update_meta)

    # update-summary
    p_summary = subparsers.add_parser("update-summary", help="Update thread summary")
    p_summary.add_argument("thread_id", help="Thread ID")
    p_summary.add_argument("summary", help="New summary text")
    p_summary.set_defaults(func=cmd_update_summary)

    # close-thread
    p_close = subparsers.add_parser("close-thread", help="Close a thread")
    p_close.add_argument("thread_id", help="Thread ID")
    p_close.set_defaults(func=cmd_close_thread)

    # show-topics
    p_show = subparsers.add_parser("show-topics", help="Show all topics")
    p_show.set_defaults(func=cmd_show_topics)

    # get-archive-candidates
    p_archive_candidates = subparsers.add_parser(
        "get-archive-candidates", help="List threads that are candidates for archiving"
    )
    p_archive_candidates.set_defaults(func=cmd_get_archive_candidates)

    # archive-thread
    p_archive = subparsers.add_parser("archive-thread", help="Archive a thread")
    p_archive.add_argument("thread_id", help="Thread ID to archive")
    p_archive.set_defaults(func=cmd_archive_thread)

    # get-shared-context
    p_get_shared = subparsers.add_parser("get-shared-context", help="Read shared context entries")
    p_get_shared.add_argument("--type", dest="type", default=None, metavar="TYPE",
                              help="Filter by type: decision, fact, note")
    p_get_shared.add_argument("--thread", dest="thread", default=None, metavar="THREAD_ID",
                              help="Filter by source thread")
    p_get_shared.add_argument("--limit", dest="limit", type=int, default=0, metavar="N",
                              help="Return at most N most-recent entries")
    p_get_shared.add_argument("--plain", dest="plain", action="store_true",
                              help="Plain text output for prompt inclusion (default: JSON)")
    p_get_shared.set_defaults(func=cmd_get_shared_context)

    # add-shared
    p_shared = subparsers.add_parser("add-shared", help="Add to shared context")
    p_shared.add_argument("--type", dest="type", required=True, metavar="TYPE")
    p_shared.add_argument("--content", dest="content", required=True, metavar="TEXT")
    p_shared.add_argument("--thread", dest="thread", default=None, metavar="SOURCE_THREAD")
    p_shared.set_defaults(func=cmd_add_shared)

    # set-agent
    p_set_agent = subparsers.add_parser("set-agent", help="Set agent task for a thread")
    p_set_agent.add_argument("thread_id", help="Thread ID")
    p_set_agent.add_argument("task_id", help="Agent task ID")
    p_set_agent.set_defaults(func=cmd_set_agent)

    # complete-agent
    p_complete = subparsers.add_parser("complete-agent", help="Mark agent task as complete")
    p_complete.add_argument("thread_id", help="Thread ID")
    p_complete.add_argument("result_summary", help="Result summary text")
    p_complete.set_defaults(func=cmd_complete_agent)

    # fail-agent
    p_fail = subparsers.add_parser("fail-agent", help="Mark agent task as failed")
    p_fail.add_argument("thread_id", help="Thread ID")
    p_fail.add_argument("error", help="Error description")
    p_fail.set_defaults(func=cmd_fail_agent)

    # check-agents
    p_check = subparsers.add_parser("check-agents", help="List background agents as JSON")
    p_check.set_defaults(func=cmd_check_agents)

    # get-context
    p_ctx = subparsers.add_parser("get-context", help="Print context string")
    p_ctx.set_defaults(func=cmd_get_context)

    # init-db
    p_init = subparsers.add_parser("init-db", help="Initialize DB schema")
    p_init.set_defaults(func=cmd_init_db)

    # set-summarized-count
    p_set_count = subparsers.add_parser("set-summarized-count", help="Set summarized message count")
    p_set_count.add_argument("thread_id")
    p_set_count.add_argument("count", type=int)
    p_set_count.set_defaults(func=cmd_set_summarized_count)

    # get-stale-threads
    p_stale = subparsers.add_parser("get-stale-threads", help="List threads with stale summaries")
    p_stale.add_argument("--threshold", type=int, default=3)
    p_stale.set_defaults(func=cmd_get_stale_threads)

    # get-messages
    p_msgs = subparsers.add_parser("get-messages", help="Show messages for a thread")
    p_msgs.add_argument("thread_id")
    p_msgs.add_argument("--limit", type=int, default=None)
    p_msgs.add_argument("--plain", action="store_true", help="Plain role: content format")
    p_msgs.set_defaults(func=cmd_get_messages)

    args = parser.parse_args()

    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
