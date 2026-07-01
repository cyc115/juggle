"""juggle_cli_commands_agents — agent-pool COMMANDS entries (P9 R3).

Ports juggle_cli_parsers_agents.register() 1:1 into declarative Cmd entries,
KEEPING legacy flat names as the canonical verb (resource=None — no rename until
G1). Handlers are the SAME objects the wall binds; ``integrate`` uses a named
wrapper mirroring the wall's inline lambda (lazy import preserved). Data only.
"""

from __future__ import annotations

from juggle_cli_spec import Arg, Cmd
from juggle_cmd_agents import (
    cmd_ack_action,
    cmd_check_agents,
    cmd_complete_agent,
    cmd_decommission_agent,
    cmd_fail_agent,
    cmd_get_agent,
    cmd_list_actions,
    cmd_list_agents,
    cmd_notify,
    cmd_release_agent,
    cmd_request_action,
    cmd_send_message,
    cmd_send_task,
    cmd_set_watchdog,
    cmd_spawn_agent,
    cmd_stop_watchdog,
)

_ROLE_CHOICES = ("researcher", "coder", "planner")


def _integrate_dispatch(a):
    # Mirrors the wall's inline lambda: lazy import keeps module load cheap.
    return __import__("juggle_cmd_integrate").cmd_integrate(a)


# NOTE (P9 G1): these tables span THREE resources — agent (pool/dispatch),
# action (action items), watchdog (daemon control) — plus the top-level flat
# `integrate`. Legacy flat names are recorded as aliases for the A1 shim.
AGENT_COMMANDS: tuple[Cmd, ...] = (
    Cmd("agent", "complete", cmd_complete_agent,
        args=(
            Arg("thread_id", help="Thread ID"),
            Arg("result_summary", help="Result summary text"),
            Arg("--retain", dest="retain_text", default=None, metavar="TEXT",
                help="Retain text (accepted but inert — auto Hindsight writes removed)"),
            Arg("--open-questions", default=None,
                help="JSON array of pending questions from planner"),
            Arg("--handoff", dest="handoff", default=None, metavar="JSON_OR_TEXT",
                help="Structured output contract for graph-task threads: files "
                     "touched, interfaces added/changed, key decisions, follow-ups. "
                     "REQUIRED when the bound graph task has dependents (DA M4)"),
            Arg("--role", dest="role", default=None, choices=_ROLE_CHOICES,
                help="Agent role override (used when agent was not registered via get-agent)"),
        ),
        aliases=("complete-agent",),
        help="Mark agent task as complete"),
    Cmd(None, "integrate", _integrate_dispatch,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("--allow-main", action="store_true", dest="allow_main", default=False,
                help="Allow integration even if worktree fields are missing (operator bypass)"),
        ),
        help="Rebase-aware atomic worktree finalization: fetch → rebase → test → ff-merge → push"),
    Cmd("agent", "fail", cmd_fail_agent,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("error", help="Error description"),
            Arg("--type", dest="failure_type", choices=("transient", "persistent"),
                default=None, help="Override auto-classification (transient/persistent)"),
            Arg("--max-retries", dest="max_retries", type=int, default=0,
                help="Retry budget for transient failures (orchestrator uses)"),
            Arg("--recovery-dispatched", action="store_true", default=False,
                help="Orchestrator dispatched a recovery agent — notify only, no action item"),
        ),
        aliases=("fail-agent",),
        help="Mark agent task as failed"),
    Cmd("action", "create", cmd_request_action,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("message", help="Action required text"),
            Arg("--type", dest="type", default="manual_step",
                choices=("question", "manual_step", "decision", "failure")),
            Arg("--priority", dest="priority", default="normal",
                choices=("low", "normal", "high")),
        ),
        aliases=("request-action",),
        help="Create a persistent action item"),
    Cmd("action", "notify", cmd_notify,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("message", help="Notification text"),
        ),
        aliases=("notify",),
        help="Surface a notification in the cockpit"),
    Cmd("action", "ack", cmd_ack_action,
        args=(Arg("action_id", help="Action item integer id"),),
        aliases=("ack-action",),
        help="Dismiss an action item"),
    Cmd("action", "list", cmd_list_actions, aliases=("list-actions",),
        help="List open action items"),
    Cmd("agent", "check", cmd_check_agents, aliases=("check-agents",),
        help="List background agents as JSON"),
    Cmd("agent", "spawn", cmd_spawn_agent,
        args=(
            Arg("role", choices=_ROLE_CHOICES),
            Arg("--model", dest="model", default="sonnet",
                help="Claude model alias or full name (default: sonnet)"),
        ),
        aliases=("spawn-agent",),
        help="Spawn a new tmux agent"),
    Cmd("agent", "list", cmd_list_agents, aliases=("list-agents",),
        help="List all tmux agents"),
    Cmd("agent", "get", cmd_get_agent,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("--role", dest="role", default=None, choices=_ROLE_CHOICES),
            Arg("--model", dest="model", default="sonnet",
                help="Claude model alias or full name (default: sonnet)"),
            Arg("--repo", dest="repo", default=None,
                help="Filter idle agents to matching repo_path (default: current cwd git toplevel)"),
            Arg("--harness", dest="harness", default=None,
                help="Request specific harness backend (default: config agent.harness)"),
            Arg("--fresh", dest="fresh", action="store_true",
                help="Force a new agent spawn, skipping idle reuse"),
        ),
        aliases=("get-agent",),
        help="Get best idle agent (or spawn new)"),
    Cmd("agent", "release", cmd_release_agent,
        args=(
            Arg("agent_id", help="Agent UUID or thread label"),
            Arg("--force", action="store_true",
                help="Force release even if thread is still active (operator use only)"),
        ),
        aliases=("release-agent",),
        help="Return agent to idle pool"),
    Cmd("agent", "decommission", cmd_decommission_agent,
        args=(Arg("agent_id", help="Agent UUID"),),
        aliases=("decommission-agent",),
        help="Kill agent pane + remove from DB"),
    Cmd("agent", "send-task", cmd_send_task,
        args=(
            Arg("agent_id", help="Agent UUID"),
            Arg("prompt_file", help="Path to prompt file"),
            Arg("--no-template", action="store_true",
                help="Skip role template prepend (use raw prompt file content only)"),
            Arg("--worktree-path", dest="worktree_path", default=None,
                help="Worktree path for orchestrator-owned finalization"),
            Arg("--worktree-branch", dest="worktree_branch", default=None,
                help="Worktree branch for orchestrator-owned finalization"),
            Arg("--main-repo-path", dest="main_repo_path", default=None,
                help="Main repo path for orchestrator-owned finalization"),
            Arg("--allow-main", action="store_true", dest="allow_main", default=False,
                help="Bypass worktree guard and allow coder/planner to run in main worktree (logged)"),
            Arg("--topic", dest="topic", default=None,
                help="Owning feature topic (label/UUID) — child task is parented here for derived close"),
        ),
        aliases=("send-task",),
        help="Send prompt file to agent pane"),
    Cmd("agent", "send-message", cmd_send_message,
        args=(
            Arg("agent_id", help="Agent UUID"),
            Arg("text", help="Message text to send to the agent"),
            Arg("--json", dest="json_out", action="store_true", help="Output result as JSON"),
        ),
        aliases=("send-message",),
        help="Send a steering message to a running agent pane"),
    Cmd("agent", "set-watchdog", cmd_set_watchdog,
        args=(Arg("agent_id"), Arg("value", help="Minutes (int) or 'off'")),
        aliases=("set-watchdog",),
        help="Set per-agent watchdog threshold or disable it"),
    Cmd("watchdog", "stop", cmd_stop_watchdog,
        args=(
            Arg("--freeze", action="store_true",
                help="Also set the freeze sentinel (no respawn until `juggle start`, "
                     "which clears the freeze and restarts the watchdog)."),
        ),
        aliases=("stop-watchdog",),
        help="Send SIGTERM to the watchdog daemon"),
)
