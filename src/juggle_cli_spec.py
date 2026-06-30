"""juggle_cli_spec — declarative CLI command spec dataclasses (P9 R1).

Single source of truth for the juggle CLI surface: the ``COMMANDS`` tuple of ``Cmd``
entries replaces the four hand-written ``add_parser`` walls. This module defines the
``Cmd``/``Arg`` dataclasses, ``Arg.add_to`` (the declarative→argparse translator),
the ``COMMANDS`` table, and the generic ``build_parser`` registrar. ``build_parser``
is PARALLEL to the four hand-written ``register()`` walls and is NOT wired into
``main()`` yet (R3 populates ``COMMANDS`` from the real handlers; R4 switches the
entrypoint over). No handler imports live here, so importing this module has zero
side effects and does not change any existing CLI behavior.

Spec: docs CLI-grammar-migration §3 (spec-table sketch).

    Cmd("thread", "create", cmd_create_thread,
        args=(Arg("topic"),), aliases=("create-thread",), help="Create a topic thread")
    Cmd(None, "verify", cmd_verify, passthrough=True)   # top-level global verb
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

# Handler imports (R3) — the SAME objects the four register() walls bind, so the
# ported COMMANDS entries dispatch identically. Eager, mirroring the walls.
from juggle_cmd_threads import (
    cmd_archive_thread,
    cmd_close_thread,
    cmd_create_thread,
    cmd_get_archive_candidates,
    cmd_get_messages,
    cmd_get_stale_threads,
    cmd_set_summarized_count,
    cmd_show_topics,
    cmd_start,
    cmd_stop,
    cmd_switch_thread,
    cmd_unarchive_thread,
    cmd_update_meta,
)
from juggle_cli_parsers_threads import _doctor_dispatch
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
from juggle_cmd_context import (
    cmd_digest,
    cmd_get_context,
    cmd_grep_vault,
    cmd_init_db,
    cmd_next_action,
    cmd_retain,
)
from juggle_cmd_misc import (
    _cmd_list_selfheal,
    _cmd_selfheal_audit,
    _cmd_selfheal_propose_nonissue,
    _cmd_selfheal_reset_diagnosing,
    _cmd_selfheal_set_status,
    _cmd_show_selfheal,
    cmd_agent_tools,
    cmd_cockpit,
)
from juggle_cmd_research import cmd_research
from juggle_cmd_db_flush import cmd_db_flush
from juggle_cmd_add_node import cmd_add_node

# Sentinel distinguishing "caller did not set this field" from a legitimate
# ``None``/falsy argparse value (e.g. ``default=None``). Hashable, so frozen
# Cmd/Arg instances stay hashable.
_UNSET: Any = object()


@dataclass(frozen=True)
class Arg:
    """One argparse argument, declared as data.

    ``name`` is a positional (``"topic"``) or an optional flag (``"--retain"``).
    Every other field maps 1:1 to an ``argparse.add_argument`` keyword and is only
    forwarded when explicitly set (``_UNSET`` fields are omitted, so argparse's own
    defaults apply). ``add_to`` performs the translation; no parser is built here.
    """

    name: str
    dest: str | None = None
    help: str = ""
    action: Any = _UNSET
    nargs: Any = _UNSET
    type: Any = _UNSET
    choices: Any = _UNSET
    default: Any = _UNSET
    const: Any = _UNSET
    required: Any = _UNSET
    metavar: Any = _UNSET

    @property
    def is_positional(self) -> bool:
        return not self.name.startswith("-")

    def add_to(self, parser) -> None:
        """Apply this argument to ``parser`` via ``add_argument``."""
        kwargs: dict[str, Any] = {}
        if self.help:
            kwargs["help"] = self.help
        # ``dest`` is only valid for optionals; positionals derive it from name.
        if self.dest is not None and not self.is_positional:
            kwargs["dest"] = self.dest
        for key in (
            "action", "nargs", "type", "choices", "default", "const",
            "required", "metavar",
        ):
            value = getattr(self, key)
            if value is not _UNSET:
                kwargs[key] = value
        parser.add_argument(self.name, **kwargs)


@dataclass(frozen=True)
class Cmd:
    """One CLI command in the uniform ``juggle <resource> <verb>`` grammar.

    ``resource is None`` marks a top-level global verb (e.g. ``start``, ``verify``,
    ``doctor``) that reads better flat. ``aliases`` holds legacy flat names the
    backward-compat shim (A1) will rewrite to ``[resource, verb]``. ``passthrough``
    flags a command parsed with ``parse_known_args`` (only ``verify`` today).
    """

    resource: str | None
    verb: str
    handler: Callable[[Any], Any]
    args: tuple[Arg, ...] = ()
    aliases: tuple[str, ...] = ()
    help: str = ""
    passthrough: bool = False


# ── handler wrappers for the wall entries that used inline lambdas ────────────
# (The other walls bind named functions, imported above.) These mirror the
# lambdas verbatim, keeping their lazy imports so module load stays cheap.


def _integrate_dispatch(a):
    return __import__("juggle_cmd_integrate").cmd_integrate(a)


def _schedule_dogfood(a):
    return __import__("schedules.dogfood", fromlist=["run"]).run(dry_run=a.dry_run) or None


def _schedule_autofix(a):
    return __import__("schedules.autofix", fromlist=["run"]).run(dry_run=a.dry_run) or None


def _schedule_reflect(a):
    return __import__("schedules.reflect", fromlist=["run"]).run(dry_run=a.dry_run) or None


_ROLE_CHOICES = ("researcher", "coder", "planner")

# The declarative command table — the four register() walls (threads/agents/misc/
# selfheal) ported 1:1. R3 KEEPS the legacy flat names as the canonical verb
# (resource=None, no rename) so behavior is identical; G1 renames to resource-verb.
# build_parser(COMMANDS) is PARALLEL + UNUSED until R4 wires it into main().
# NOTE: project/graph/project-graph/runs/autopilot groups + the entry-module verbs
# (verify/vault-path/vault-name/open-in-editor) are NOT walls and are out of scope.
# Two known fidelity gaps vs the walls, both invisible to live behavior (unused):
#   - grep-vault --vault-path default is the runtime-resolved vault path, injected
#     by the entry point; here it defaults to None (R4 must re-inject it).
#   - list-selfheal --group/--flat are a mutually-exclusive group in the wall; the
#     Cmd/Arg model has no mutex-group concept yet, so they are plain flags here.
COMMANDS: tuple[Cmd, ...] = (
    # ── thread / session lifecycle (juggle_cli_parsers_threads) ──────────────
    Cmd(None, "start", cmd_start,
        args=(Arg("--session-id", dest="session_id", default=None),),
        help="Start juggle mode"),
    Cmd(None, "stop", cmd_stop, help="Stop juggle mode"),
    Cmd(None, "create-thread", cmd_create_thread,
        args=(Arg("topic", help="Topic name"),),
        help="Create a new topic thread"),
    Cmd(None, "doctor", _doctor_dispatch,
        args=(
            Arg("--dry-run", action="store_true", help="Print actions; write nothing"),
            Arg("--pre-p8-check", action="store_true", dest="pre_p8_check",
                help="Report remaining legacy-table refs (static) + nodes mirror "
                     "readiness (runtime); exit nonzero until both clear"),
            Arg("--json", action="store_true", dest="json_out",
                help="Emit --pre-p8-check result as JSON"),
        ),
        help="Migrate config + DB to current schema"),
    Cmd(None, "switch-thread", cmd_switch_thread,
        args=(Arg("thread_id", help="Thread ID (e.g. A, B, C)"),),
        help="Switch to a topic thread"),
    Cmd(None, "update-meta", cmd_update_meta,
        args=(
            Arg("thread_id", help="Thread ID"),
            Arg("--add-decision", dest="add_decision", default=None, metavar="TEXT"),
            Arg("--add-question", dest="add_question", default=None, metavar="TEXT"),
            Arg("--resolve-question", dest="resolve_question", default=None, metavar="TEXT"),
        ),
        help="Update thread metadata"),
    Cmd(None, "close-thread", cmd_close_thread,
        args=(Arg("thread_id", help="Thread ID"),), help="Close a thread"),
    Cmd(None, "show-topics", cmd_show_topics, help="Show all topics"),
    Cmd(None, "get-archive-candidates", cmd_get_archive_candidates,
        help="List threads that are candidates for archiving"),
    Cmd(None, "archive-thread", cmd_archive_thread,
        args=(Arg("thread_id", help="Thread ID to archive"),), help="Archive a thread"),
    Cmd(None, "unarchive-thread", cmd_unarchive_thread,
        args=(Arg("thread_id", help="Thread ID to unarchive (label or UUID)"),),
        help="Unarchive a thread"),
    Cmd(None, "set-summarized-count", cmd_set_summarized_count,
        args=(Arg("thread_id"), Arg("count", type=int)),
        help="Set summarized message count"),
    Cmd(None, "get-stale-threads", cmd_get_stale_threads,
        args=(Arg("--threshold", type=int, default=3),),
        help="List threads with stale summaries"),
    Cmd(None, "get-messages", cmd_get_messages,
        args=(
            Arg("thread_id"),
            Arg("--limit", type=int, default=None),
            Arg("--plain", action="store_true", help="Plain role: content format"),
        ),
        help="Show messages for a thread"),

    # ── agent pool / dispatch / actions / watchdog (juggle_cli_parsers_agents) ─
    Cmd(None, "complete-agent", cmd_complete_agent,
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
        help="Mark agent task as complete"),
    Cmd(None, "integrate", _integrate_dispatch,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("--allow-main", action="store_true", dest="allow_main", default=False,
                help="Allow integration even if worktree fields are missing (operator bypass)"),
        ),
        help="Rebase-aware atomic worktree finalization: fetch → rebase → test → ff-merge → push"),
    Cmd(None, "fail-agent", cmd_fail_agent,
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
        help="Mark agent task as failed"),
    Cmd(None, "request-action", cmd_request_action,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("message", help="Action required text"),
            Arg("--type", dest="type", default="manual_step",
                choices=("question", "manual_step", "decision", "failure")),
            Arg("--priority", dest="priority", default="normal",
                choices=("low", "normal", "high")),
        ),
        help="Create a persistent action item"),
    Cmd(None, "notify", cmd_notify,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("message", help="Notification text"),
        ),
        help="Surface a notification in the cockpit"),
    Cmd(None, "ack-action", cmd_ack_action,
        args=(Arg("action_id", help="Action item integer id"),),
        help="Dismiss an action item"),
    Cmd(None, "list-actions", cmd_list_actions, help="List open action items"),
    Cmd(None, "check-agents", cmd_check_agents, help="List background agents as JSON"),
    Cmd(None, "spawn-agent", cmd_spawn_agent,
        args=(
            Arg("role", choices=_ROLE_CHOICES),
            Arg("--model", dest="model", default="sonnet",
                help="Claude model alias or full name (default: sonnet)"),
        ),
        help="Spawn a new tmux agent"),
    Cmd(None, "list-agents", cmd_list_agents, help="List all tmux agents"),
    Cmd(None, "get-agent", cmd_get_agent,
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
        help="Get best idle agent (or spawn new)"),
    Cmd(None, "release-agent", cmd_release_agent,
        args=(
            Arg("agent_id", help="Agent UUID or thread label"),
            Arg("--force", action="store_true",
                help="Force release even if thread is still active (operator use only)"),
        ),
        help="Return agent to idle pool"),
    Cmd(None, "decommission-agent", cmd_decommission_agent,
        args=(Arg("agent_id", help="Agent UUID"),),
        help="Kill agent pane + remove from DB"),
    Cmd(None, "send-task", cmd_send_task,
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
        ),
        help="Send prompt file to agent pane"),
    Cmd(None, "send-message", cmd_send_message,
        args=(
            Arg("agent_id", help="Agent UUID"),
            Arg("text", help="Message text to send to the agent"),
            Arg("--json", dest="json_out", action="store_true", help="Output result as JSON"),
        ),
        help="Send a steering message to a running agent pane"),
    Cmd(None, "set-watchdog", cmd_set_watchdog,
        args=(Arg("agent_id"), Arg("value", help="Minutes (int) or 'off'")),
        help="Set per-agent watchdog threshold or disable it"),
    Cmd(None, "stop-watchdog", cmd_stop_watchdog,
        args=(
            Arg("--freeze", action="store_true",
                help="Also set the freeze sentinel (no respawn until `juggle start`, "
                     "which clears the freeze and restarts the watchdog)."),
        ),
        help="Send SIGTERM to the watchdog daemon"),

    # ── context / memory / research / schedule / cockpit / node / db (misc) ───
    Cmd(None, "get-context", cmd_get_context, help="Print context string"),
    Cmd(None, "init-db", cmd_init_db, help="Initialize DB schema"),
    Cmd(None, "agent-tools", cmd_agent_tools,
        args=(
            Arg("--role", default=None, help="Filter to one role"),
            Arg("--reset", action="store_true", help="Clear recorded tool-usage data"),
        ),
        help="Report per-agent tool usage to right-size the deny block"),
    Cmd(None, "grep-vault", cmd_grep_vault,
        args=(
            Arg("terms", nargs="+", help="Search terms (max 5)"),
            # default re-injected by the entry point in R4 (was vault_path_default).
            Arg("--vault-path", default=None, help="Vault path to search"),
        ),
        help="Search vault for terms (file paths only)"),
    Cmd(None, "retain", cmd_retain,
        args=(
            Arg("thread_id", help="Thread ID or label"),
            Arg("content", help="Content to retain"),
            Arg("--context", dest="context", default=None,
                help="Context type: learnings, procedures, preferences"),
        ),
        help="Retain content as memory"),
    Cmd(None, "digest", cmd_digest,
        args=(
            Arg("--since", dest="since", default="yesterday",
                help="Cutoff: ISO timestamp, 'today', or 'yesterday' (default)"),
            Arg("--save", action="store_true",
                help="Write to ~/.juggle/logs/juggle-digest-YYYY-MM-DD.md"),
        ),
        help="Summarize activity since last session"),
    Cmd(None, "next-action", cmd_next_action,
        help="Switch to highest-priority action item"),
    Cmd(None, "research", cmd_research,
        args=(
            Arg("topic", help="Research topic"),
            Arg("--no-web", action="store_true"),
            Arg("--verbose", action="store_true"),
            Arg("--web-results", dest="web_results", default=None),
        ),
        help="Search research KB"),
    Cmd(None, "schedule-dogfood", _schedule_dogfood,
        args=(Arg("--dry-run", action="store_true"),),
        help="Run /schedule:dogfood routine (Sat 03:00)"),
    Cmd(None, "schedule-autofix", _schedule_autofix,
        args=(Arg("--dry-run", action="store_true"),),
        help="Run /schedule:autofix routine (Sun 03:00)"),
    Cmd(None, "schedule-reflect", _schedule_reflect,
        args=(Arg("--dry-run", action="store_true"),),
        help="Run /schedule:reflect routine (Mon 03:00)"),
    Cmd(None, "cockpit", cmd_cockpit,
        args=(
            Arg("--db", dest="db_path", default=None, help="Path to juggle.db"),
            Arg("--out", action="store_true",
                help="Render panes as plain text to stdout then exit (no TUI)"),
            Arg("--profile", action="store_true",
                help="Run headless resource-usage profiling loop (no TUI)"),
            Arg("--duration", type=int, default=60, metavar="N",
                help="Duration in seconds for --profile (default: 60)"),
            Arg("--screenshot", metavar="PATH", help="Save PNG/JPG/SVG screenshot to PATH"),
            Arg("--legend", action="store_true",
                help="Print the ? help overlay (keys + glyph legend) to stdout then exit (no TUI)"),
            Arg("--smoke", action="store_true",
                help="Run viewport smoke test matrix (renders all profiles via pty+pyte)"),
            Arg("--viewport", dest="viewport_name", metavar="NAME", default=None,
                help="Smoke-test a single named viewport profile (e.g. 2k_third)"),
            Arg("--all-viewports", action="store_true",
                help="Smoke-test all viewport profiles (default when --smoke is given)"),
            Arg("--interactive", action="store_true",
                help="Also exercise keyboard nav, resize, and UI flows during smoke"),
            Arg("--json", dest="json_out", action="store_true",
                help="Output smoke results as JSON"),
            Arg("--smoke-graph", dest="smoke_graph", action="store_true",
                help="During smoke, toggle the lower-right panel into Graph mode (press g) and validate it"),
        ),
        help="Open live cockpit dashboard"),
    Cmd(None, "add-node", cmd_add_node,
        args=(
            Arg("title", help="Node title"),
            Arg("--kind", default="task",
                choices=("task", "research", "conversation", "decision"),
                help="Node kind (default: task)"),
            Arg("--objective", default=None,
                help="Objective / prompt (omit or '-' to read from stdin)"),
            Arg("--project", default=None, help="Project id tag (default: INBOX)"),
            Arg("--deps", default=None, help="Comma-separated node ids this node depends on"),
            Arg("--required-by", dest="required_by", default=None,
                help="Comma-separated node ids that gain a dep on this node"),
            Arg("--verify-cmd", dest="verify_cmd", default=None,
                help="Verification command (task only)"),
            Arg("--parent", default=None, help="Parent node id (sub-task)"),
            Arg("--json", dest="json_out", action="store_true", help="Emit {node_id: ...} JSON"),
        ),
        help="Create a unified graph node (P5)"),
    Cmd(None, "db-flush", cmd_db_flush,
        args=(
            Arg("--once", action="store_true", help="Single flush then exit"),
            Arg("--status", action="store_true", help="Print last-flush status JSON"),
            Arg("--live", default=None, help="Override live DB path"),
            Arg("--durable", default=None, help="Override durable DB path"),
            Arg("--interval", type=float, default=None, help="Override flush interval (s)"),
            Arg("--install-supervisor", action="store_true", dest="install_supervisor",
                help="Write systemd/launchd supervisor config"),
        ),
        help="Flush live (tmpfs) DB to durable disk path"),

    # ── self-heal triage family (juggle_cli_parsers_selfheal) ─────────────────
    Cmd(None, "list-selfheal", _cmd_list_selfheal,
        args=(
            Arg("--json", action="store_true", default=False, help="Output as JSON array"),
            Arg("--all", action="store_true", default=False,
                help="Include resolved + non_issue rows"),
            Arg("--status", default=None, help="Filter to exactly one status (e.g. non_issue)"),
            Arg("--group", action="store_true", default=False,
                help="Grouped (group_key) view — DEFAULT"),
            Arg("--flat", action="store_true", default=False,
                help="Flat exact-signature rows (pre-P2 behavior)"),
        ),
        help="List pending self-heal errors"),
    Cmd(None, "show-selfheal", _cmd_show_selfheal,
        args=(
            Arg("id", type=int, help="error_events.id"),
            Arg("--json", action="store_true", default=False,
                help="Output the full row as a JSON object"),
        ),
        help="Show one error_event's full detail (command_args + traceback + status + counts)"),
    Cmd(None, "selfheal-audit", _cmd_selfheal_audit,
        args=(
            Arg("--json", action="store_true", default=False, help="Output as JSON array"),
            Arg("--action", default=None,
                help="Filter to one action (allowlist_hide|resurface|silent_autohide|lease_set|new_variant)"),
            Arg("--limit", type=int, default=50, help="Max rows (default 50)"),
        ),
        help="Show the self-heal audit log"),
    Cmd(None, "selfheal-set-status", _cmd_selfheal_set_status,
        args=(
            Arg("id", type=int, help="error_events.id"),
            Arg("status",
                help="open|diagnosing|awaiting_approval|non_issue_proposed|non_issue|resolved"),
            Arg("--action-item-id", type=int, dest="action_item_id", default=None),
        ),
        help="Update error_event status"),
    Cmd(None, "selfheal-reset-diagnosing", _cmd_selfheal_reset_diagnosing,
        args=(Arg("id", type=int, help="error_events.id"),),
        help="Reset stuck diagnosing->open"),
    Cmd(None, "selfheal-propose-nonissue", _cmd_selfheal_propose_nonissue,
        args=(Arg("id", type=int, help="error_events.id"),),
        help="Mark an error_event as non_issue_proposed (visible benign proposal)"),
)


def build_parser(
    commands: Iterable[Cmd] = COMMANDS, *, prog: str = "juggle"
) -> argparse.ArgumentParser:
    """Build an argparse parser from declarative ``Cmd`` entries (§3).

    Top-level global verbs (``resource is None``) attach directly under the root
    subparsers; resource-scoped commands group under a per-resource subparsers
    object (``juggle <resource> <verb>``), created once per resource and reused.
    Each leaf gets its declared ``Arg``\\s and ``set_defaults(func=handler)``.

    Legacy ``aliases`` are intentionally NOT registered here — the backward-compat
    layer (A1) rewrites legacy argv to ``[resource, verb]`` BEFORE this parser runs,
    so the parser tree stays canonical-only. ``passthrough`` likewise is consumed at
    dispatch time (``parse_known_args``), not expressed in the tree.

    PARALLEL + UNUSED: nothing calls this yet; ``main()`` still uses the hand-written
    walls until R4.
    """
    parser = argparse.ArgumentParser(prog=prog)
    sub = parser.add_subparsers(dest="command", required=True)
    groups: dict[str, Any] = {}  # resource -> its add_subparsers() object
    for c in commands:
        if c.resource is None:
            leaf = sub.add_parser(c.verb, help=c.help)
        else:
            group = groups.get(c.resource)
            if group is None:
                resource_parser = sub.add_parser(
                    c.resource, help=f"{c.resource} commands"
                )
                group = resource_parser.add_subparsers(
                    dest=f"{c.resource}_command", required=True
                )
                groups[c.resource] = group
            leaf = group.add_parser(c.verb, help=c.help)
        for arg in c.args:
            arg.add_to(leaf)
        leaf.set_defaults(func=c.handler)
    return parser
