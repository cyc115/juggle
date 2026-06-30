"""juggle_cli_commands_misc — context/memory/research/schedule/cockpit/node/db
COMMANDS entries (P9 R3).

Ports the flat commands of juggle_cli_parsers_misc.register() 1:1 into declarative
Cmd entries, KEEPING legacy flat names as the canonical verb (resource=None — no
rename until G1). The 3 schedule routines use named wrappers mirroring the wall's
inline lambdas (lazy imports preserved). Data only.

OUT OF SCOPE (not flat walls — already grouped/conformant, registered elsewhere):
project, project-graph, graph, runs, autopilot.
KNOWN GAP (unused until R4, zero live impact): grep-vault --vault-path default is
the runtime-resolved vault path injected by the entry point; here it is None and
R4 must re-inject it.
"""

from __future__ import annotations

from juggle_cli_spec import Arg, Cmd
from juggle_cmd_context import (
    cmd_digest,
    cmd_get_context,
    cmd_grep_vault,
    cmd_init_db,
    cmd_next_action,
    cmd_retain,
)
from juggle_cmd_misc import cmd_agent_tools, cmd_cockpit
from juggle_cmd_research import cmd_research
from juggle_cmd_db_flush import cmd_db_flush
from juggle_cmd_add_node import cmd_add_node


def _schedule_dogfood(a):
    return __import__("schedules.dogfood", fromlist=["run"]).run(dry_run=a.dry_run) or None


def _schedule_autofix(a):
    return __import__("schedules.autofix", fromlist=["run"]).run(dry_run=a.dry_run) or None


def _schedule_reflect(a):
    return __import__("schedules.reflect", fromlist=["run"]).run(dry_run=a.dry_run) or None


MISC_COMMANDS: tuple[Cmd, ...] = (
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
)
