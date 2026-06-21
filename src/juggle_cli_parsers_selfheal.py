"""
juggle_cli_parsers_selfheal — Subparser registration for the self-heal
error_events triage command family.

Owns: argparse wiring only for list-selfheal / show-selfheal / selfheal-audit /
selfheal-set-status / selfheal-reset-diagnosing / selfheal-propose-nonissue.
Extracted from juggle_cli_parsers_misc (2026-06-21) to keep that module under
the LOC gate and give the selfheal CLI surface its own domain seam. P2 added the
grouped view flags + the selfheal-audit command.
Must not own: command handler logic (lives in juggle_cmd_misc).
"""

from juggle_cmd_misc import (
    _cmd_list_selfheal,
    _cmd_selfheal_audit,
    _cmd_selfheal_propose_nonissue,
    _cmd_selfheal_reset_diagnosing,
    _cmd_selfheal_set_status,
    _cmd_show_selfheal,
)


def register_selfheal_parsers(subparsers) -> None:
    """Register the self-heal error_events triage subcommands."""
    p_list_selfheal = subparsers.add_parser("list-selfheal", help="List pending self-heal errors")
    p_list_selfheal.add_argument("--json", action="store_true", default=False, help="Output as JSON array")
    p_list_selfheal.add_argument("--all", action="store_true", default=False,
                                 help="Include resolved + non_issue rows")
    p_list_selfheal.add_argument("--status", default=None,
                                 help="Filter to exactly one status (e.g. non_issue)")
    _view = p_list_selfheal.add_mutually_exclusive_group()
    _view.add_argument("--group", action="store_true", default=False,
                       help="Grouped (group_key) view — DEFAULT")
    _view.add_argument("--flat", action="store_true", default=False,
                       help="Flat exact-signature rows (pre-P2 behavior)")
    p_list_selfheal.set_defaults(func=_cmd_list_selfheal)

    p_show_selfheal = subparsers.add_parser(
        "show-selfheal",
        help="Show one error_event's full detail (command_args + traceback + status + counts)")
    p_show_selfheal.add_argument("id", type=int, help="error_events.id")
    p_show_selfheal.add_argument("--json", action="store_true", default=False,
                                 help="Output the full row as a JSON object")
    p_show_selfheal.set_defaults(func=_cmd_show_selfheal)

    p_sh_audit = subparsers.add_parser("selfheal-audit", help="Show the self-heal audit log")
    p_sh_audit.add_argument("--json", action="store_true", default=False, help="Output as JSON array")
    p_sh_audit.add_argument("--action", default=None,
                            help="Filter to one action (allowlist_hide|resurface|silent_autohide|lease_set|new_variant)")
    p_sh_audit.add_argument("--limit", type=int, default=50, help="Max rows (default 50)")
    p_sh_audit.set_defaults(func=_cmd_selfheal_audit)

    p_sh_set = subparsers.add_parser("selfheal-set-status", help="Update error_event status")
    p_sh_set.add_argument("id", type=int, help="error_events.id")
    p_sh_set.add_argument("status",
                          help="open|diagnosing|awaiting_approval|non_issue_proposed|non_issue|resolved")
    p_sh_set.add_argument("--action-item-id", type=int, dest="action_item_id", default=None)
    p_sh_set.set_defaults(func=_cmd_selfheal_set_status)

    p_sh_reset = subparsers.add_parser("selfheal-reset-diagnosing", help="Reset stuck diagnosing->open")
    p_sh_reset.add_argument("id", type=int, help="error_events.id")
    p_sh_reset.set_defaults(func=_cmd_selfheal_reset_diagnosing)

    p_sh_propose = subparsers.add_parser(
        "selfheal-propose-nonissue",
        help="Mark an error_event as non_issue_proposed (visible benign proposal)")
    p_sh_propose.add_argument("id", type=int, help="error_events.id")
    p_sh_propose.set_defaults(func=_cmd_selfheal_propose_nonissue)
