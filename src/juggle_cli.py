#!/usr/bin/env python3
"""
Juggle CLI - called by LLM via Bash tool for state changes.
Usage: python juggle_cli.py <command> [args]
"""

import argparse
import os
import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

# Re-export commonly used symbols for backward compatibility with tests
from juggle_cli_common import (  # noqa: F401
    _extract_decision_prompt,
    _last_sentences,
    get_db,
)

from juggle_cmd_threads import (
    cmd_start,
    cmd_stop,
    cmd_create_thread,
    cmd_switch_thread,
    cmd_update_meta,
    cmd_update_summary,
    cmd_close_thread,
    cmd_show_topics,
    cmd_get_archive_candidates,
    cmd_archive_thread,
    cmd_unarchive_thread,
    cmd_set_summarized_count,
    cmd_get_stale_threads,
    cmd_get_messages,
)

from juggle_cmd_agents import (
    cmd_set_agent,
    cmd_complete_agent,
    cmd_fail_agent,
    cmd_check_agents,
    cmd_spawn_agent,
    cmd_list_agents,
    cmd_get_agent,
    cmd_release_agent,
    cmd_decommission_agent,
    cmd_send_task,
    cmd_request_action,
    cmd_ack_action,
    cmd_list_actions,
    cmd_notify,
)

from juggle_cmd_context import (
    cmd_get_context,
    cmd_init_db,
    cmd_recall,
    cmd_recall_bg,
    cmd_recall_if_cold,
    cmd_retain,
    cmd_grep_vault,
    cmd_register_domain,
    cmd_register_domain_path,
    cmd_digest,
    cmd_next_action,
)


def cmd_record_pending_decision(args):
    """Record pending user decisions in current thread's open_questions."""
    import json
    db = get_db()

    thread = db.get_current_thread()
    if not thread:
        return

    thread_obj = db.get_thread(thread)

    try:
        questions = json.loads(args.questions_json)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in --questions-json: {e}", file=sys.stderr)
        sys.exit(1)

    open_questions = thread_obj.get("open_questions") or []
    if isinstance(open_questions, str):
        open_questions = json.loads(open_questions)

    for i, q in enumerate(questions):
        if "q" not in q:
            print(f"Error: question {i} missing 'q' field", file=sys.stderr)
            sys.exit(1)
        open_questions.append({
            "id": f"{args.tool_use_id}:{i}",
            "text": q["q"],
            "source": "askuser",
        })

    db.update_thread(thread, open_questions=open_questions)


def cmd_clear_pending_decision(args):
    """Clear pending decisions by tool_use_id prefix."""
    import json
    db = get_db()

    thread = db.get_current_thread()
    if not thread:
        return

    thread_obj = db.get_thread(thread)
    open_questions = thread_obj.get("open_questions") or []
    if isinstance(open_questions, str):
        open_questions = json.loads(open_questions)

    open_questions = [q for q in open_questions if not q.get("id", "").startswith(args.tool_use_id)]

    db.update_thread(thread, open_questions=open_questions)


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
    p_create.add_argument("--domain", dest="domain", default=None,
                          help="Domain tag for agent isolation (e.g. 'juggle', 'vault', 'work')")
    p_create.set_defaults(func=cmd_create_thread)

    # register-domain
    p_reg_domain = subparsers.add_parser("register-domain", help="Register a new domain name")
    p_reg_domain.add_argument("name", help="Domain name (e.g. 'juggle', 'vault')")
    p_reg_domain.set_defaults(func=cmd_register_domain)

    # register-domain-path
    p_reg_path = subparsers.add_parser("register-domain-path",
                                        help="Map a path fragment to a domain for auto-detection")
    p_reg_path.add_argument("path_fragment", help="Path substring (e.g. '/github/juggle')")
    p_reg_path.add_argument("domain", help="Domain name to map to")
    p_reg_path.set_defaults(func=cmd_register_domain_path)

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

    # unarchive-thread
    p_unarchive = subparsers.add_parser("unarchive-thread", help="Unarchive a thread")
    p_unarchive.add_argument("thread_id", help="Thread ID to unarchive (label or UUID)")
    p_unarchive.set_defaults(func=cmd_unarchive_thread)

    # set-agent
    p_set_agent = subparsers.add_parser("set-agent", help="Set agent task for a thread")
    p_set_agent.add_argument("thread_id", help="Thread ID")
    p_set_agent.add_argument("task_id", help="Agent task ID")
    p_set_agent.set_defaults(func=cmd_set_agent)

    # complete-agent
    p_complete = subparsers.add_parser("complete-agent", help="Mark agent task as complete")
    p_complete.add_argument("thread_id", help="Thread ID")
    p_complete.add_argument("result_summary", help="Result summary text")
    p_complete.add_argument(
        "--retain",
        dest="retain_text",
        default=None,
        metavar="TEXT",
        help="Explicit retain text (key decisions, personal details, non-obvious learnings)",
    )
    p_complete.add_argument(
        "--open-questions",
        default=None,
        help="JSON array of pending questions from planner",
    )
    p_complete.add_argument(
        "--role",
        dest="role",
        default=None,
        choices=["researcher", "coder", "planner"],
        help="Agent role override (used when agent was not registered via get-agent)",
    )
    p_complete.set_defaults(func=cmd_complete_agent)

    # fail-agent
    p_fail = subparsers.add_parser("fail-agent", help="Mark agent task as failed")
    p_fail.add_argument("thread_id", help="Thread ID or label")
    p_fail.add_argument("error", help="Error description")
    p_fail.add_argument("--type", dest="failure_type",
                        choices=["transient", "persistent"], default=None,
                        help="Override auto-classification (transient/persistent)")
    p_fail.add_argument("--max-retries", dest="max_retries", type=int, default=0,
                        help="Retry budget for transient failures (orchestrator uses)")
    p_fail.add_argument("--recovery-dispatched", action="store_true", default=False,
                        help="Orchestrator dispatched a recovery agent — notify only, no action item")
    p_fail.set_defaults(func=cmd_fail_agent)

    # request-action
    p_req = subparsers.add_parser("request-action", help="Create a persistent action item")
    p_req.add_argument("thread_id", help="Thread ID or label")
    p_req.add_argument("message", help="Action required text")
    p_req.add_argument("--type", dest="type", default="manual_step",
                       choices=["question", "manual_step", "decision", "failure"])
    p_req.add_argument("--priority", dest="priority", default="normal",
                       choices=["low", "normal", "high"])
    p_req.set_defaults(func=cmd_request_action)

    # notify
    p_notify = subparsers.add_parser("notify", help="Surface a notification in the cockpit")
    p_notify.add_argument("thread_id", help="Thread ID or label")
    p_notify.add_argument("message", help="Notification text")
    p_notify.set_defaults(func=cmd_notify)

    # ack-action
    p_ack = subparsers.add_parser("ack-action", help="Dismiss an action item")
    p_ack.add_argument("action_id", help="Action item integer id")
    p_ack.set_defaults(func=cmd_ack_action)

    # list-actions
    p_list_actions = subparsers.add_parser("list-actions", help="List open action items")
    p_list_actions.set_defaults(func=cmd_list_actions)

    # check-agents
    p_check = subparsers.add_parser("check-agents", help="List background agents as JSON")
    p_check.set_defaults(func=cmd_check_agents)

    # spawn-agent
    p_spawn = subparsers.add_parser("spawn-agent", help="Spawn a new tmux agent")
    p_spawn.add_argument("role", choices=["researcher", "coder", "planner"])
    p_spawn.add_argument("--model", dest="model", default="sonnet",
                          help="Claude model alias or full name (default: sonnet)")
    p_spawn.set_defaults(func=cmd_spawn_agent)

    # list-agents
    p_list_agents = subparsers.add_parser("list-agents", help="List all tmux agents")
    p_list_agents.set_defaults(func=cmd_list_agents)

    # get-agent
    p_get_agent = subparsers.add_parser("get-agent", help="Get best idle agent (or spawn new)")
    p_get_agent.add_argument("thread_id", help="Thread ID or label")
    p_get_agent.add_argument("--role", dest="role", default=None,
                              choices=["researcher", "coder", "planner"])
    p_get_agent.add_argument("--model", dest="model", default="sonnet",
                              help="Claude model alias or full name (default: sonnet)")
    p_get_agent.set_defaults(func=cmd_get_agent)

    # release-agent
    p_release = subparsers.add_parser("release-agent", help="Return agent to idle pool")
    p_release.add_argument("agent_id", help="Agent UUID or thread label")
    p_release.add_argument("--force", action="store_true",
                           help="Force release even if thread is still active (operator use only)")
    p_release.set_defaults(func=cmd_release_agent)

    # decommission-agent
    p_decommission = subparsers.add_parser("decommission-agent", help="Kill agent pane + remove from DB")
    p_decommission.add_argument("agent_id", help="Agent UUID")
    p_decommission.set_defaults(func=cmd_decommission_agent)

    # send-task
    p_send_task = subparsers.add_parser("send-task", help="Send prompt file to agent pane")
    p_send_task.add_argument("agent_id", help="Agent UUID")
    p_send_task.add_argument("prompt_file", help="Path to prompt file")
    p_send_task.set_defaults(func=cmd_send_task)

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

    # recall
    p_recall = subparsers.add_parser("recall", help="Recall memories from Hindsight")
    p_recall.add_argument("thread_id", help="Thread ID or label")
    p_recall.add_argument("query", help="Query to recall memories for")
    p_recall.set_defaults(func=cmd_recall)

    # recall-bg
    p_recall_bg = subparsers.add_parser("recall-bg", help="Fire reflect async, return immediately")
    p_recall_bg.add_argument("thread_id")
    p_recall_bg.add_argument("query")
    p_recall_bg.set_defaults(func=cmd_recall_bg)

    # recall-if-cold
    p_recall_cold = subparsers.add_parser("recall-if-cold", help="Recall only if thread is cold")
    p_recall_cold.add_argument("thread_id", help="Thread ID or label")
    p_recall_cold.add_argument("query", help="Query to recall memories for")
    p_recall_cold.set_defaults(func=cmd_recall_if_cold)

    # grep-vault
    p_grep = subparsers.add_parser("grep-vault", help="Search vault for terms (file paths only)")
    p_grep.add_argument("terms", nargs="+", help="Search terms (max 5)")
    p_grep.add_argument("--vault-path", default=str(Path.home() / "Documents" / "personal"),
                        help="Vault path to search")
    p_grep.set_defaults(func=cmd_grep_vault)

    # retain
    p_retain = subparsers.add_parser("retain", help="Retain content as memory")
    p_retain.add_argument("thread_id", help="Thread ID or label")
    p_retain.add_argument("content", help="Content to retain")
    p_retain.add_argument("--context", dest="context", default=None,
                          help="Context type: learnings, procedures, preferences")
    p_retain.set_defaults(func=cmd_retain)

    # digest
    p_digest = subparsers.add_parser("digest", help="Summarize activity since last session")
    p_digest.add_argument("--since", dest="since", default="yesterday",
                          help="Cutoff: ISO timestamp, 'today', or 'yesterday' (default)")
    p_digest.add_argument("--save", action="store_true",
                          help="Write to ~/.juggle/logs/juggle-digest-YYYY-MM-DD.md")
    p_digest.set_defaults(func=cmd_digest)

    # next-action
    p_next = subparsers.add_parser("next-action", help="Switch to highest-priority action item")
    p_next.set_defaults(func=cmd_next_action)

    # record-pending-decision
    record_parser = subparsers.add_parser("record-pending-decision", help="Record pending user decisions")
    record_parser.add_argument("--tool-use-id", required=True)
    record_parser.add_argument("--questions-json", required=True)
    record_parser.set_defaults(func=cmd_record_pending_decision)

    # clear-pending-decision
    clear_parser = subparsers.add_parser("clear-pending-decision", help="Clear pending decisions by tool_use_id")
    clear_parser.add_argument("--tool-use-id", required=True)
    clear_parser.set_defaults(func=cmd_clear_pending_decision)

    args = parser.parse_args()

    # Reap stale agents on every CLI invocation (skip in test mode)
    if "_JUGGLE_TEST_DB" not in os.environ:
        try:
            from juggle_tmux import reap_stale_agents, JuggleTmuxManager
            _reap_db = get_db()
            _reap_mgr = JuggleTmuxManager()
            reap_stale_agents(_reap_db, _reap_mgr)
        except Exception:
            pass  # Non-fatal; reaper can be skipped

    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
