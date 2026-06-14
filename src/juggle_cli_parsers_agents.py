"""
juggle_cli_parsers_agents — Subparser registration for agent pool commands.

Owns: argparse wiring for agent lifecycle, task dispatch, completion/failure,
action items, notify, integrate, and watchdog control.
Must not own: command handler logic (lives in juggle_cmd_agents_*).
"""

from juggle_cmd_agents import (
    cmd_complete_agent,
    cmd_fail_agent,
    cmd_check_agents,
    cmd_spawn_agent,
    cmd_list_agents,
    cmd_get_agent,
    cmd_release_agent,
    cmd_decommission_agent,
    cmd_send_task,
    cmd_send_message,
    cmd_request_action,
    cmd_ack_action,
    cmd_list_actions,
    cmd_notify,
    cmd_set_watchdog,
    cmd_stop_watchdog,
)


def register(subparsers) -> None:
    """Register agent-pool subcommands on the given subparsers object."""
    # complete-agent
    p_complete = subparsers.add_parser(
        "complete-agent", help="Mark agent task as complete"
    )
    p_complete.add_argument("thread_id", help="Thread ID")
    p_complete.add_argument("result_summary", help="Result summary text")
    p_complete.add_argument(
        "--retain",
        dest="retain_text",
        default=None,
        metavar="TEXT",
        help="Retain text (accepted but inert — auto Hindsight writes removed)",
    )
    p_complete.add_argument(
        "--open-questions",
        default=None,
        help="JSON array of pending questions from planner",
    )
    p_complete.add_argument(
        "--handoff",
        dest="handoff",
        default=None,
        metavar="JSON_OR_TEXT",
        help="Structured output contract for graph-task threads: files touched, "
        "interfaces added/changed, key decisions, follow-ups. REQUIRED when "
        "the bound graph task has dependents (DA M4)",
    )
    p_complete.add_argument(
        "--role",
        dest="role",
        default=None,
        choices=["researcher", "coder", "planner"],
        help="Agent role override (used when agent was not registered via get-agent)",
    )
    p_complete.set_defaults(func=cmd_complete_agent)

    # integrate
    p_integrate = subparsers.add_parser(
        "integrate", help="Rebase-aware atomic worktree finalization: fetch → rebase → test → ff-merge → push"
    )
    p_integrate.add_argument("thread_id", help="Thread ID or label")
    p_integrate.add_argument(
        "--allow-main",
        action="store_true",
        dest="allow_main",
        default=False,
        help="Allow integration even if worktree fields are missing (operator bypass)",
    )
    p_integrate.set_defaults(
        func=lambda a: __import__("juggle_cmd_integrate").cmd_integrate(a)
    )

    # fail-agent
    p_fail = subparsers.add_parser("fail-agent", help="Mark agent task as failed")
    p_fail.add_argument("thread_id", help="Thread ID or label")
    p_fail.add_argument("error", help="Error description")
    p_fail.add_argument(
        "--type",
        dest="failure_type",
        choices=["transient", "persistent"],
        default=None,
        help="Override auto-classification (transient/persistent)",
    )
    p_fail.add_argument(
        "--max-retries",
        dest="max_retries",
        type=int,
        default=0,
        help="Retry budget for transient failures (orchestrator uses)",
    )
    p_fail.add_argument(
        "--recovery-dispatched",
        action="store_true",
        default=False,
        help="Orchestrator dispatched a recovery agent — notify only, no action item",
    )
    p_fail.set_defaults(func=cmd_fail_agent)

    # request-action
    p_req = subparsers.add_parser(
        "request-action", help="Create a persistent action item"
    )
    p_req.add_argument("thread_id", help="Thread ID or label")
    p_req.add_argument("message", help="Action required text")
    p_req.add_argument(
        "--type",
        dest="type",
        default="manual_step",
        choices=["question", "manual_step", "decision", "failure"],
    )
    p_req.add_argument(
        "--priority",
        dest="priority",
        default="normal",
        choices=["low", "normal", "high"],
    )
    p_req.set_defaults(func=cmd_request_action)

    # notify
    p_notify = subparsers.add_parser(
        "notify", help="Surface a notification in the cockpit"
    )
    p_notify.add_argument("thread_id", help="Thread ID or label")
    p_notify.add_argument("message", help="Notification text")
    p_notify.set_defaults(func=cmd_notify)

    # ack-action
    p_ack = subparsers.add_parser("ack-action", help="Dismiss an action item")
    p_ack.add_argument("action_id", help="Action item integer id")
    p_ack.set_defaults(func=cmd_ack_action)

    # list-actions
    p_list_actions = subparsers.add_parser(
        "list-actions", help="List open action items"
    )
    p_list_actions.set_defaults(func=cmd_list_actions)

    # check-agents
    p_check = subparsers.add_parser(
        "check-agents", help="List background agents as JSON"
    )
    p_check.set_defaults(func=cmd_check_agents)

    # spawn-agent
    p_spawn = subparsers.add_parser("spawn-agent", help="Spawn a new tmux agent")
    p_spawn.add_argument("role", choices=["researcher", "coder", "planner"])
    p_spawn.add_argument(
        "--model",
        dest="model",
        default="sonnet",
        help="Claude model alias or full name (default: sonnet)",
    )
    p_spawn.set_defaults(func=cmd_spawn_agent)

    # list-agents
    p_list_agents = subparsers.add_parser("list-agents", help="List all tmux agents")
    p_list_agents.set_defaults(func=cmd_list_agents)

    # get-agent
    p_get_agent = subparsers.add_parser(
        "get-agent", help="Get best idle agent (or spawn new)"
    )
    p_get_agent.add_argument("thread_id", help="Thread ID or label")
    p_get_agent.add_argument(
        "--role", dest="role", default=None, choices=["researcher", "coder", "planner"]
    )
    p_get_agent.add_argument(
        "--model",
        dest="model",
        default="sonnet",
        help="Claude model alias or full name (default: sonnet)",
    )
    p_get_agent.add_argument(
        "--repo",
        dest="repo",
        default=None,
        help="Filter idle agents to matching repo_path (default: current cwd git toplevel)",
    )
    p_get_agent.add_argument(
        "--harness",
        dest="harness",
        default=None,
        help="Request specific harness backend (default: config agent.harness)",
    )
    p_get_agent.add_argument(
        "--fresh",
        dest="fresh",
        action="store_true",
        help="Force a new agent spawn, skipping idle reuse",
    )
    p_get_agent.set_defaults(func=cmd_get_agent)

    # release-agent
    p_release = subparsers.add_parser("release-agent", help="Return agent to idle pool")
    p_release.add_argument("agent_id", help="Agent UUID or thread label")
    p_release.add_argument(
        "--force",
        action="store_true",
        help="Force release even if thread is still active (operator use only)",
    )
    p_release.set_defaults(func=cmd_release_agent)

    # decommission-agent
    p_decommission = subparsers.add_parser(
        "decommission-agent", help="Kill agent pane + remove from DB"
    )
    p_decommission.add_argument("agent_id", help="Agent UUID")
    p_decommission.set_defaults(func=cmd_decommission_agent)

    # send-task
    p_send_task = subparsers.add_parser(
        "send-task", help="Send prompt file to agent pane"
    )
    p_send_task.add_argument("agent_id", help="Agent UUID")
    p_send_task.add_argument("prompt_file", help="Path to prompt file")
    p_send_task.add_argument(
        "--no-template",
        action="store_true",
        help="Skip role template prepend (use raw prompt file content only)",
    )
    p_send_task.add_argument(
        "--worktree-path",
        dest="worktree_path",
        default=None,
        help="Worktree path for orchestrator-owned finalization",
    )
    p_send_task.add_argument(
        "--worktree-branch",
        dest="worktree_branch",
        default=None,
        help="Worktree branch for orchestrator-owned finalization",
    )
    p_send_task.add_argument(
        "--main-repo-path",
        dest="main_repo_path",
        default=None,
        help="Main repo path for orchestrator-owned finalization",
    )
    p_send_task.add_argument(
        "--allow-main",
        action="store_true",
        dest="allow_main",
        default=False,
        help="Bypass worktree guard and allow coder/planner to run in main worktree (logged)",
    )
    p_send_task.add_argument(
        # '--force-node' is a DEPRECATED hidden alias of '--force-task', baked
        # into the autopilot hook + global CLAUDE.md — it MUST keep working.
        "--force-task",
        "--force-node",
        action="store_true",
        dest="force_task",
        default=False,
        help="Bypass the graph-task guard: dispatch to a thread bound to a "
        "tick-owned graph task (the autopilot tick uses this internally)",
    )
    p_send_task.set_defaults(func=cmd_send_task)

    # send-message
    p_send_msg = subparsers.add_parser(
        "send-message", help="Send a steering message to a running agent pane"
    )
    p_send_msg.add_argument("agent_id", help="Agent UUID")
    p_send_msg.add_argument("text", help="Message text to send to the agent")
    p_send_msg.add_argument(
        "--json",
        dest="json_out",
        action="store_true",
        help="Output result as JSON",
    )
    p_send_msg.set_defaults(func=cmd_send_message)

    # set-watchdog
    p_set_watchdog = subparsers.add_parser(
        "set-watchdog", help="Set per-agent watchdog threshold or disable it"
    )
    p_set_watchdog.add_argument("agent_id")
    p_set_watchdog.add_argument("value", help="Minutes (int) or 'off'")
    p_set_watchdog.set_defaults(func=cmd_set_watchdog)

    # stop-watchdog
    p_stop_watchdog = subparsers.add_parser(
        "stop-watchdog", help="Send SIGTERM to the watchdog daemon"
    )
    p_stop_watchdog.set_defaults(func=cmd_stop_watchdog)
