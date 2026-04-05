#!/usr/bin/env python3
"""
Juggle CLI - called by LLM via Bash tool for state changes.
Usage: python juggle_cli.py <command> [args]
"""

import argparse
import json
import os
import sys
from pathlib import Path

_DATA_DIR = Path(os.environ.get("CLAUDE_PLUGIN_DATA", Path.home() / ".claude" / "juggle"))
DB_PATH = _DATA_DIR / "juggle.db"
SRC_DIR = Path(__file__).parent


def get_db():
    sys.path.insert(0, str(SRC_DIR))
    from juggle_db import JuggleDB
    return JuggleDB(str(DB_PATH))


def cmd_start(args):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    db = get_db()
    db.init_db()
    db.set_active(True)

    threads = db.get_all_threads()
    if not threads:
        thread_id = db.create_thread("General", session_id="")
        db.set_current_thread(thread_id)
        print(f"Juggle started. Topic {thread_id} created. Use 'create-thread <topic>' to create more topics.")
    else:
        current = db.get_current_thread()
        if not current and threads:
            db.set_current_thread(threads[0]["thread_id"])
        print("Juggle started.")


def cmd_stop(args):
    db = get_db()
    db.set_active(False)

    threads = db.get_all_threads()
    if threads:
        print("Topics:")
        for t in threads:
            print(f"  [{t['thread_id']}] {t['topic']} — {t['status']}")
    else:
        print("No topics.")

    print("Juggle stopped.")


def cmd_create_thread(args):
    db = get_db()
    thread_id = db.create_thread(args.topic, session_id="")
    db.set_current_thread(thread_id)
    print(f"Created Topic {thread_id}: {args.topic}. Now in Topic {thread_id}.")


def cmd_switch_thread(args):
    db = get_db()
    thread = db.get_thread(args.thread_id)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.set_current_thread(args.thread_id)

    print(f"=== Topic {thread['thread_id']}: {thread['topic']} ===")
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

    messages = db.get_messages(args.thread_id, token_budget=2000)
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
    thread = db.get_thread(args.thread_id)
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
        db.update_thread(args.thread_id, key_decisions=key_decisions)

    if args.add_question:
        open_questions = thread.get("open_questions") or "[]"
        if isinstance(open_questions, str):
            try:
                open_questions = json.loads(open_questions)
            except (json.JSONDecodeError, ValueError):
                open_questions = []
        open_questions.append(args.add_question)
        db.update_thread(args.thread_id, open_questions=open_questions)

    if args.resolve_question:
        open_questions = thread.get("open_questions") or "[]"
        if isinstance(open_questions, str):
            try:
                open_questions = json.loads(open_questions)
            except (json.JSONDecodeError, ValueError):
                open_questions = []
        open_questions = [q for q in open_questions if q != args.resolve_question]
        db.update_thread(args.thread_id, open_questions=open_questions)

    if args.intent:
        db.update_thread(args.thread_id, last_user_intent=args.intent)

    print(f"Updated metadata for Thread {args.thread_id}.")


def cmd_update_summary(args):
    db = get_db()
    thread = db.get_thread(args.thread_id)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.update_thread(args.thread_id, summary=args.summary)
    print(f"Summary updated for Thread {args.thread_id}.")


def cmd_close_thread(args):
    db = get_db()
    thread = db.get_thread(args.thread_id)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.update_thread(args.thread_id, status="closed")
    print(f"Thread {args.thread_id} ({thread['topic']}) closed.")


def cmd_show_topics(args):
    db = get_db()
    threads = db.get_all_threads()
    if not threads:
        print("No topics.")
        return

    current = db.get_current_thread()
    print("Topics:")
    for t in threads:
        marker = "<- you are here" if t["thread_id"] == current else ""
        agent_info = ""
        if t["status"] == "background" and t.get("agent_task_id"):
            agent_info = "-> agent running..."
        elif t["status"] == "done":
            agent_info = "done (results ready)"
        elif t["status"] == "failed":
            agent_info = "failed"

        suffix = agent_info or marker
        suffix_str = f"  {suffix}" if suffix else ""
        print(f"  [{t['thread_id']}] {t['topic']} — {t['status']}{suffix_str}")


def cmd_add_shared(args):
    db = get_db()
    db.add_shared(args.type, args.content, source_thread=args.thread)
    print(f"Added [{args.type}]: {args.content}")


def cmd_set_agent(args):
    db = get_db()
    thread = db.get_thread(args.thread_id)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.update_thread(args.thread_id, agent_task_id=args.task_id, status="background")
    print(f"Thread {args.thread_id} agent task set: {args.task_id}")


def cmd_complete_agent(args):
    db = get_db()
    thread = db.get_thread(args.thread_id)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.update_thread(args.thread_id, agent_result=args.result_summary, status="done")

    notification = (
        f"[Topic {args.thread_id} completed] {thread['topic']} — results ready. "
        f"Use: python juggle_cli.py switch-thread {args.thread_id}"
    )
    db.add_notification(args.thread_id, notification)
    print(f"Thread {args.thread_id} agent completed.")


def cmd_fail_agent(args):
    db = get_db()
    thread = db.get_thread(args.thread_id)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    db.update_thread(args.thread_id, status="failed", agent_result=args.error)

    notification = f"[Topic {args.thread_id} failed] {thread['topic']} — {args.error}"
    db.add_notification(args.thread_id, notification)
    print(f"Thread {args.thread_id} agent failed.")


def cmd_check_agents(args):
    db = get_db()
    threads = db.get_all_threads()
    background = [
        {"thread_id": t["thread_id"], "task_id": t.get("agent_task_id", ""), "topic": t["topic"]}
        for t in threads
        if t["status"] == "background"
    ]
    print(json.dumps(background))


def cmd_get_context(args):
    sys.path.insert(0, str(SRC_DIR))
    from juggle_context import build_context_string
    result = build_context_string(db_path=str(DB_PATH))
    print(result)


def cmd_init_db(args):
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

    args = parser.parse_args()

    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
