#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["rich", "httpx"]
# ///
"""
Juggle CLI - called by LLM via Bash tool for state changes.
Usage: python juggle_cli.py <command> [args]
"""

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

# Load ~/.juggle/.env before any module-level code reads env vars.
# This makes OPENROUTER_KEY available to title_gen's Tier 1 path.
_ENV_FILE = Path.home() / ".juggle" / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# File-based logging — always active so background title_gen/hindsight paths are visible.
_LOG_DIR = Path.home() / ".juggle" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "juggle-cli.log"),
    ],
)

from juggle_settings import get_settings

NVIM_SOCKET = "/tmp/juggle-nvim.sock"


def _get_vault_root() -> Path:
    vault_val = get_settings()["paths"].get("vault", "/Documents/personal")
    if vault_val.startswith("~"):
        return Path(vault_val).expanduser()
    return Path.home() / vault_val.lstrip("/")


def _get_vault_name() -> str:
    explicit = get_settings()["paths"].get("vault_name", "")
    if explicit:
        return explicit
    return _get_vault_root().name


def cmd_vault_path(args):
    """Print the absolute vault root path (single source of truth for commands)."""
    print(str(_get_vault_root()))


def cmd_vault_name(args):
    """Print the vault name (used for obsidian:// URIs)."""
    print(_get_vault_name())


VAULT_ROOT = _get_vault_root()

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
    cmd_set_watchdog,
    cmd_stop_watchdog,
)

from juggle_cmd_context import (
    cmd_get_context,
    cmd_init_db,
    cmd_recall,
    cmd_recall_bg,
    cmd_recall_if_cold,
    cmd_retain,
    cmd_grep_vault,
    cmd_digest,
    cmd_next_action,
)

from juggle_cmd_research import cmd_research

from juggle_cmd_projects import (
    cmd_project_list,
    cmd_project_show,
    cmd_project_assign,
    cmd_project_edit,
    cmd_project_create,
    cmd_project_critique,
    cmd_project_close,
    cmd_project_open,
)


def cmd_cockpit(args):
    """Launch the Juggle Cockpit dashboard (Textual, mouse drag-to-resize).

    With --out: render all panes as plain text to stdout then exit (no TUI).
    With --profile: run headless resource-usage profiling harness (no TUI).
    """
    import subprocess as _sp
    src = Path(__file__).parent
    script = src / "juggle_cockpit.py"
    cmd = ["uv", "run", str(script)]
    if getattr(args, "db_path", None):
        cmd += ["--db", args.db_path]
    if getattr(args, "out", False):
        cmd += ["--out"]
    elif getattr(args, "screenshot", None):
        cmd += ["--screenshot", args.screenshot]
    elif getattr(args, "profile", False):
        cmd += ["--profile", "--duration", str(getattr(args, "duration", 60))]
    sys.exit(_sp.call(cmd))


def _obsidian_fallback(abs_file: str) -> None:
    """Open via Obsidian (vault files) or macOS system open (non-vault files)."""
    try:
        rel = Path(abs_file).relative_to(VAULT_ROOT)
        url = f"obsidian://open?vault={_get_vault_name()}&file={rel}"
        subprocess.run(["open", url], check=True)
    except ValueError:
        # File is outside the vault — Obsidian can't open it.
        print(f"nvim socket unavailable; opening with system default: {abs_file}")
        subprocess.run(["open", abs_file], check=True)


def _parse_path_with_line(spec: str) -> tuple[str, int | None]:
    """Split 'path:line' or 'path:line:col' into (path, line). Returns (spec, None) if no line."""
    m = re.match(r"^(.*?):(\d+)(?::\d+)?$", spec)
    if m:
        return m.group(1), int(m.group(2))
    return spec, None


def cmd_open_in_editor(args):
    path, line = _parse_path_with_line(args.file)
    abs_file = os.path.abspath(path)
    if os.path.exists(NVIM_SOCKET):
        try:
            subprocess.run(
                ["nvim", "--server", NVIM_SOCKET, "--remote", abs_file], check=True
            )
            if line is not None:
                subprocess.run(
                    [
                        "nvim",
                        "--server",
                        NVIM_SOCKET,
                        "--remote-send",
                        f"<C-\\><C-N>:{line}<CR>",
                    ],
                    check=True,
                )
            return
        except subprocess.CalledProcessError:
            pass
    _obsidian_fallback(abs_file)


def _deny_matches(tool_name: str, deny_list) -> bool:
    """True if tool_name is covered by a deny entry (exact or `prefix*` wildcard)."""
    for entry in deny_list or []:
        if entry.endswith("*"):
            if tool_name.startswith(entry[:-1]):
                return True
        elif tool_name == entry:
            return True
    return False


def cmd_agent_tools(args):
    """Report per-agent tool usage to systematically right-size the deny block.

    For each role it lists what tools the role actually used (with counts), and
    cross-references against that role's CONFIGURED deny to surface the two
    signals you need to tune the block:
      * over-aggressive  — a tool the role USED but its deny list strips (only
        visible from audit-mode runs); candidate to ALLOW.
      * too-loose        — a tool other roles use that this role never does and
        isn't denied; candidate to DENY.
    """
    import juggle_agent_settings as jas

    db = get_db(getattr(args, "db_path", None), init=True)

    if getattr(args, "reset", False):
        n = db.reset_agent_tool_usage()
        print(f"Cleared {n} agent tool-usage row(s).")
        return

    rows = db.get_agent_tool_usage(getattr(args, "role", None))
    if not rows:
        print(
            "No agent tool usage recorded yet.\n"
            "Dispatch agents (set agent.audit_mode=true first to relax per-role\n"
            "denies and measure true demand), then re-run this report."
        )
        return

    by_role: dict[str, list[dict]] = {}
    for r in rows:
        by_role.setdefault(r["role"], []).append(r)
    # Universe of tools any role used — proxy for "available" tools, since a
    # stripped tool never appears here.
    universe = {r["tool_name"] for r in rows}

    print("Agent tool usage  (mode: normal=steady-state, audit=denies relaxed)")
    for role in sorted(by_role):
        used = by_role[role]
        used_names = {u["tool_name"] for u in used}
        try:
            deny = (jas.build_agent_overlay(role).get("permissions") or {}).get("deny") or []
        except Exception:
            deny = []

        print(f"\n── {role} ──")
        for u in used:
            flag = ""
            if _deny_matches(u["tool_name"], deny):
                flag = "  ⚠ denied for this role but used → consider ALLOWING"
            sample = f"   {u['last_input']}" if u["last_input"] else ""
            print(f"  {u['tool_name']:<38} x{u['count']:<5} ({u['mode']}){flag}{sample}")

        candidates = sorted(
            t for t in universe if t not in used_names and not _deny_matches(t, deny)
        )
        if candidates:
            print("  candidates to DENY (used by other roles, never by this one):")
            for t in candidates:
                print(f"    {t}")


def _cmd_list_selfheal(args):
    from pathlib import Path as _Path
    db = get_db(getattr(args, "db_path", None), init=True)
    rows = db.get_open_error_events()
    if not rows:
        print("No pending self-heal errors.")
        return
    for row in rows:
        sig8 = (row["signature_hash"] or "")[:8]
        cls = row["error_class"]
        status = row["status"]
        count = row["count"]
        last = (row["last_seen"] or "")[:16]
        if cls == "A":
            detail = f"{row['exc_type'] or '?'} in {row['entrypoint'] or '?'}"
        else:
            ref = _Path(row["juggle_ref"] or "").name or row["juggle_ref"] or "?"
            detail = f"{row['entrypoint'] or '?'} error via {ref}"
        print(f"{row['id']:>4}  [{cls}]  {status:<20} count={count}  last={last}  sig={sig8}  {detail}")


def _cmd_selfheal_set_status(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    valid = ("open", "diagnosing", "awaiting_approval", "resolved")
    if args.status not in valid:
        print(f"error: invalid status {args.status!r}; choose from {valid}")
        sys.exit(1)
    updated = db.set_error_event_status(args.id, args.status, action_item_id=args.action_item_id)
    if updated:
        print(f"error_event {args.id} status \u2192 {args.status}")
    else:
        print(f"error: row {args.id} not found")
        sys.exit(1)


def _cmd_selfheal_reset_diagnosing(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    with db._connect() as conn:
        row = conn.execute(
            "SELECT status FROM error_events WHERE id = ?", (args.id,)
        ).fetchone()
    if not row:
        print(f"error: row {args.id} not found")
        sys.exit(1)
    if row["status"] != "diagnosing":
        print(f"error: row {args.id} not in diagnosing state (current: {row['status']})")
        sys.exit(1)
    db.set_error_event_status(args.id, "open")
    print(f"reset error_event {args.id} diagnosing\u2192open")



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

    # doctor — auto-migrate config + DB to current schema
    p_doctor = subparsers.add_parser(
        "doctor", help="Migrate config + DB to current schema"
    )
    p_doctor.add_argument(
        "--dry-run", action="store_true", help="Print actions; write nothing"
    )
    p_doctor.set_defaults(func=lambda a: __import__("juggle_cmd_doctor").cmd_doctor(a))

    # switch-thread
    p_switch = subparsers.add_parser("switch-thread", help="Switch to a topic thread")
    p_switch.add_argument("thread_id", help="Thread ID (e.g. A, B, C)")
    p_switch.set_defaults(func=cmd_switch_thread)

    # update-meta
    p_meta = subparsers.add_parser("update-meta", help="Update thread metadata")
    p_meta.add_argument("thread_id", help="Thread ID")
    p_meta.add_argument(
        "--add-decision", dest="add_decision", default=None, metavar="TEXT"
    )
    p_meta.add_argument(
        "--add-question", dest="add_question", default=None, metavar="TEXT"
    )
    p_meta.add_argument(
        "--resolve-question", dest="resolve_question", default=None, metavar="TEXT"
    )
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
    p_send_task.set_defaults(func=cmd_send_task)

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

    # get-context
    p_ctx = subparsers.add_parser("get-context", help="Print context string")
    p_ctx.set_defaults(func=cmd_get_context)

    # init-db
    p_init = subparsers.add_parser("init-db", help="Initialize DB schema")
    p_init.set_defaults(func=cmd_init_db)

    # set-summarized-count
    p_set_count = subparsers.add_parser(
        "set-summarized-count", help="Set summarized message count"
    )
    p_set_count.add_argument("thread_id")
    p_set_count.add_argument("count", type=int)
    p_set_count.set_defaults(func=cmd_set_summarized_count)

    # get-stale-threads
    p_stale = subparsers.add_parser(
        "get-stale-threads", help="List threads with stale summaries"
    )
    p_stale.add_argument("--threshold", type=int, default=3)
    p_stale.set_defaults(func=cmd_get_stale_threads)

    # get-messages
    p_msgs = subparsers.add_parser("get-messages", help="Show messages for a thread")
    p_msgs.add_argument("thread_id")
    p_msgs.add_argument("--limit", type=int, default=None)
    p_msgs.add_argument(
        "--plain", action="store_true", help="Plain role: content format"
    )
    p_msgs.set_defaults(func=cmd_get_messages)

    # recall
    p_recall = subparsers.add_parser("recall", help="Recall memories from Hindsight")
    p_recall.add_argument("thread_id", help="Thread ID or label")
    p_recall.add_argument("query", help="Query to recall memories for")
    p_recall.set_defaults(func=cmd_recall)

    # recall-bg
    p_recall_bg = subparsers.add_parser(
        "recall-bg", help="Fire reflect async, return immediately"
    )
    p_recall_bg.add_argument("thread_id")
    p_recall_bg.add_argument("query")
    p_recall_bg.set_defaults(func=cmd_recall_bg)

    # agent tool-usage report (right-size the deny block)
    p_agent_tools = subparsers.add_parser(
        "agent-tools", help="Report per-agent tool usage to right-size the deny block"
    )
    p_agent_tools.add_argument("--role", default=None, help="Filter to one role")
    p_agent_tools.add_argument(
        "--reset", action="store_true", help="Clear recorded tool-usage data"
    )
    p_agent_tools.set_defaults(func=cmd_agent_tools)

    # selfheal subcommands
    p_list_selfheal = subparsers.add_parser("list-selfheal", help="List pending self-heal errors")
    p_list_selfheal.set_defaults(func=_cmd_list_selfheal)

    p_sh_set = subparsers.add_parser("selfheal-set-status", help="Update error_event status")
    p_sh_set.add_argument("id", type=int, help="error_events.id")
    p_sh_set.add_argument("status", help="open|diagnosing|awaiting_approval|resolved")
    p_sh_set.add_argument("--action-item-id", type=int, dest="action_item_id", default=None)
    p_sh_set.set_defaults(func=_cmd_selfheal_set_status)

    p_sh_reset = subparsers.add_parser("selfheal-reset-diagnosing", help="Reset stuck diagnosing->open")
    p_sh_reset.add_argument("id", type=int, help="error_events.id")
    p_sh_reset.set_defaults(func=_cmd_selfheal_reset_diagnosing)

    # recall-if-cold
    p_recall_cold = subparsers.add_parser(
        "recall-if-cold", help="Recall only if thread is cold"
    )
    p_recall_cold.add_argument("thread_id", help="Thread ID or label")
    p_recall_cold.add_argument("query", help="Query to recall memories for")
    p_recall_cold.set_defaults(func=cmd_recall_if_cold)

    # grep-vault
    p_grep = subparsers.add_parser(
        "grep-vault", help="Search vault for terms (file paths only)"
    )
    p_grep.add_argument("terms", nargs="+", help="Search terms (max 5)")
    p_grep.add_argument(
        "--vault-path", default=str(_get_vault_root()), help="Vault path to search"
    )
    p_grep.set_defaults(func=cmd_grep_vault)

    # retain
    p_retain = subparsers.add_parser("retain", help="Retain content as memory")
    p_retain.add_argument("thread_id", help="Thread ID or label")
    p_retain.add_argument("content", help="Content to retain")
    p_retain.add_argument(
        "--context",
        dest="context",
        default=None,
        help="Context type: learnings, procedures, preferences",
    )
    p_retain.set_defaults(func=cmd_retain)

    # digest
    p_digest = subparsers.add_parser(
        "digest", help="Summarize activity since last session"
    )
    p_digest.add_argument(
        "--since",
        dest="since",
        default="yesterday",
        help="Cutoff: ISO timestamp, 'today', or 'yesterday' (default)",
    )
    p_digest.add_argument(
        "--save",
        action="store_true",
        help="Write to ~/.juggle/logs/juggle-digest-YYYY-MM-DD.md",
    )
    p_digest.set_defaults(func=cmd_digest)

    # next-action
    p_next = subparsers.add_parser(
        "next-action", help="Switch to highest-priority action item"
    )
    p_next.set_defaults(func=cmd_next_action)

    # open-in-editor
    p_open = subparsers.add_parser("open-in-editor", help="Open file in nvim server")
    p_open.add_argument("file", help="Path to file to open")
    p_open.set_defaults(func=cmd_open_in_editor)

    # vault-path / vault-name (single source of truth for commands resolving the vault)
    p_vault_path = subparsers.add_parser("vault-path", help="Print absolute vault root path")
    p_vault_path.set_defaults(func=cmd_vault_path)
    p_vault_name = subparsers.add_parser("vault-name", help="Print vault name (for obsidian:// URIs)")
    p_vault_name.set_defaults(func=cmd_vault_name)

    # research
    p_research = subparsers.add_parser("research", help="Search research KB")
    p_research.add_argument("topic", help="Research topic")
    p_research.add_argument("--no-web", action="store_true")
    p_research.add_argument("--verbose", action="store_true")
    p_research.add_argument("--web-results", dest="web_results", default=None)
    p_research.set_defaults(func=cmd_research)

    # schedule-dogfood (Sat 03:00 local / 0 8 * * 6 UTC)
    p_dogfood = subparsers.add_parser(
        "schedule-dogfood", help="Run /schedule:dogfood routine (Sat 03:00)"
    )
    p_dogfood.add_argument("--dry-run", action="store_true")
    p_dogfood.set_defaults(
        func=lambda a: (
            __import__("juggle_schedule_dogfood").run(dry_run=a.dry_run) or None
        )
    )

    # schedule-autofix (Sun 03:00 local / 0 8 * * 0 UTC)
    p_autofix = subparsers.add_parser(
        "schedule-autofix", help="Run /schedule:autofix routine (Sun 03:00)"
    )
    p_autofix.add_argument("--dry-run", action="store_true")
    p_autofix.set_defaults(
        func=lambda a: (
            __import__("juggle_schedule_autofix").run(dry_run=a.dry_run) or None
        )
    )

    # cockpit
    p_cockpit = subparsers.add_parser("cockpit", help="Open live cockpit dashboard")
    p_cockpit.add_argument("--db", dest="db_path", default=None, help="Path to juggle.db")
    p_cockpit.add_argument(
        "--out",
        action="store_true",
        help="Render panes as plain text to stdout then exit (no TUI)",
    )
    p_cockpit.add_argument(
        "--profile",
        action="store_true",
        help="Run headless resource-usage profiling loop (no TUI)",
    )
    p_cockpit.add_argument(
        "--duration",
        type=int,
        default=60,
        metavar="N",
        help="Duration in seconds for --profile (default: 60)",
    )
    p_cockpit.add_argument("--screenshot", metavar="PATH", help="Save PNG/JPG/SVG screenshot to PATH")
    p_cockpit.set_defaults(func=cmd_cockpit)

    # schedule-reflect (Mon 03:00 local / 0 8 * * 1 UTC)
    p_reflect = subparsers.add_parser(
        "schedule-reflect", help="Run /schedule:reflect routine (Mon 03:00)"
    )
    p_reflect.add_argument("--dry-run", action="store_true")
    p_reflect.set_defaults(
        func=lambda a: (
            __import__("juggle_schedule_reflect").run(dry_run=a.dry_run) or None
        )
    )

    # juggle project <subcmd>
    p_project = subparsers.add_parser("project", help="Manage projects")
    _ps = p_project.add_subparsers(dest="project_command", required=True)

    _p = _ps.add_parser("list")
    _p.set_defaults(func=cmd_project_list)

    _p = _ps.add_parser("show")
    _p.add_argument("project_id")
    _p.set_defaults(func=cmd_project_show)

    _p = _ps.add_parser("assign")
    _p.add_argument("thread_id")
    _p.add_argument("project_id")
    _p.set_defaults(func=cmd_project_assign)

    _p = _ps.add_parser("edit")
    _p.add_argument("project_id")
    _p.add_argument("--name")
    _p.add_argument("--objective")
    _p.add_argument("--out-of-scope", dest="out_of_scope")
    _p.set_defaults(func=cmd_project_edit)

    _p = _ps.add_parser("create")
    _p.add_argument("--force", action="store_true")
    _p.add_argument("--name")
    _p.add_argument("--objective")
    _p.add_argument("--success-criteria", dest="success_criteria")
    _p.add_argument("--out-of-scope", dest="out_of_scope", default="")
    _p.set_defaults(func=cmd_project_create)

    _p = _ps.add_parser("critique")
    _p.add_argument("project_id")
    _p.set_defaults(func=cmd_project_critique)

    _p = _ps.add_parser("close")
    _p.add_argument("project_id", nargs="+")
    _p.set_defaults(func=cmd_project_close)

    _p = _ps.add_parser("open")
    _p.add_argument("project_id", nargs="+")
    _p.set_defaults(func=cmd_project_open)

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
        from juggle_selfheal import record_error
        record_error(e, "juggle_cli.main", {"argv": sys.argv})
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
