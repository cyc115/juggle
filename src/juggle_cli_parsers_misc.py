"""
juggle_cli_parsers_misc — Subparser registration for context/memory, selfheal,
research, schedules, cockpit, agent-tools, and project commands.

Owns: argparse wiring only.
Must not own: command handler logic (lives in juggle_cmd_context,
juggle_cmd_misc, juggle_cmd_research, juggle_cmd_projects, schedule modules).
"""

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
from juggle_cmd_misc import (
    _cmd_list_selfheal,
    _cmd_selfheal_reset_diagnosing,
    _cmd_selfheal_set_status,
    cmd_agent_tools,
    cmd_cockpit,
)
from juggle_cmd_projects import (
    cmd_project_list,
    cmd_project_show,
    cmd_project_assign,
    cmd_project_edit,
    cmd_project_create,
    cmd_project_critique,
    cmd_project_close,
    cmd_project_open,
    cmd_project_synth,
)
from juggle_cmd_research import cmd_research


def register(subparsers, *, vault_path_default: str) -> None:
    """Register context/memory/selfheal/research/schedule/cockpit/project
    subcommands on the given subparsers object.

    vault_path_default: default for grep-vault --vault-path (resolved by the
    entry point so this module never imports juggle_cli).
    """
    # get-context
    p_ctx = subparsers.add_parser("get-context", help="Print context string")
    p_ctx.set_defaults(func=cmd_get_context)

    # init-db
    p_init = subparsers.add_parser("init-db", help="Initialize DB schema")
    p_init.set_defaults(func=cmd_init_db)

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
        "--vault-path", default=vault_path_default, help="Vault path to search"
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
    p_cockpit.add_argument(
        "--smoke",
        action="store_true",
        help="Run viewport smoke test matrix (renders all profiles via pty+pyte)",
    )
    p_cockpit.add_argument(
        "--viewport",
        dest="viewport_name",
        metavar="NAME",
        default=None,
        help="Smoke-test a single named viewport profile (e.g. 2k_third)",
    )
    p_cockpit.add_argument(
        "--all-viewports",
        action="store_true",
        help="Smoke-test all viewport profiles (default when --smoke is given)",
    )
    p_cockpit.add_argument(
        "--interactive",
        action="store_true",
        help="Also exercise keyboard nav, resize, and UI flows during smoke",
    )
    p_cockpit.add_argument(
        "--json",
        dest="json_out",
        action="store_true",
        help="Output smoke results as JSON",
    )
    p_cockpit.set_defaults(func=cmd_cockpit)

    # juggle project <subcmd>
    p_project = subparsers.add_parser("project", help="Manage projects")
    _ps = p_project.add_subparsers(dest="project_command", required=True)

    _p = _ps.add_parser("list")
    _p.set_defaults(func=cmd_project_list)

    _p = _ps.add_parser("show")
    _p.add_argument("project_id")
    _p.set_defaults(func=cmd_project_show)

    _p = _ps.add_parser("assign")
    _p.add_argument("thread_id", nargs="+", help="One or more thread labels/UUIDs; last arg is project_id")
    _p.set_defaults(func=cmd_project_assign)

    _p = _ps.add_parser("edit")
    _p.add_argument("project_id")
    _p.add_argument("--name")
    _p.add_argument("--objective")
    _p.add_argument("--out-of-scope", dest="out_of_scope")
    _p.add_argument("--success-criterion", dest="success_criterion", action="append", metavar="CRITERION")
    _p.add_argument("--success-criteria-json", dest="success_criteria_json", metavar="JSON")
    _p.add_argument("--clear-success-criteria", dest="clear_success_criteria", action="store_true")
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

    _p = _ps.add_parser("synth", help="Synthesize match_profile for project(s)")
    _synth_group = _p.add_mutually_exclusive_group()
    _synth_group.add_argument("--all", action="store_true", help="Re-synth all active projects")
    _synth_group.add_argument("--dirty", action="store_true", help="Re-synth only dirty projects")
    _p.add_argument("project_id", nargs="?", help="Project id (omit if --all or --dirty)")
    _p.set_defaults(func=cmd_project_synth)
