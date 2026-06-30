"""juggle_cli_parsers_project — argparse wiring for the `project <verb>` group.

Relocated verbatim from juggle_cli_parsers_misc.register() when P9 R4 deleted the
four flat walls. The `project` group is already noun-verb conformant (NOT one of
the ported flat walls), so it keeps imperative registration — main() calls this
alongside register_graph_parsers / register_runs_parsers / autopilot.register.
Must not own: command handler logic (lives in juggle_cmd_projects).
"""

from juggle_cmd_projects import (
    cmd_project_assign,
    cmd_project_close,
    cmd_project_create,
    cmd_project_critique,
    cmd_project_edit,
    cmd_project_list,
    cmd_project_open,
    cmd_project_show,
    cmd_project_synth,
)


def register_project_parsers(subparsers) -> None:
    """Register the `project <verb>` subcommand group on ``subparsers``."""
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
