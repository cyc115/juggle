"""juggle_cli_commands_selfheal — self-heal triage COMMANDS entries (P9 R3).

Ports juggle_cli_parsers_selfheal.register_selfheal_parsers() 1:1 into declarative
Cmd entries, KEEPING legacy flat names as the canonical verb (resource=None — no
rename until G1). Handlers are the SAME objects the wall binds. Data only.

KNOWN GAP (unused until R4, zero live impact): list-selfheal --group/--flat are a
mutually-exclusive group in the wall; the Cmd/Arg model has no mutex-group concept
yet, so they are plain flags here (same option strings + dests).
"""

from __future__ import annotations

from juggle_cli_spec import Arg, Cmd
from juggle_cmd_misc import (
    _cmd_list_selfheal,
    _cmd_selfheal_audit,
    _cmd_selfheal_propose_nonissue,
    _cmd_selfheal_reset_diagnosing,
    _cmd_selfheal_set_status,
    _cmd_show_selfheal,
)

SELFHEAL_COMMANDS: tuple[Cmd, ...] = (
    Cmd("selfheal", "list", _cmd_list_selfheal,
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
        aliases=("list-selfheal",),
        help="List pending self-heal errors"),
    Cmd("selfheal", "show", _cmd_show_selfheal,
        args=(
            Arg("id", type=int, help="error_events.id"),
            Arg("--json", action="store_true", default=False,
                help="Output the full row as a JSON object"),
        ),
        aliases=("show-selfheal",),
        help="Show one error_event's full detail (command_args + traceback + status + counts)"),
    Cmd("selfheal", "audit", _cmd_selfheal_audit,
        args=(
            Arg("--json", action="store_true", default=False, help="Output as JSON array"),
            Arg("--action", default=None,
                help="Filter to one action (allowlist_hide|resurface|silent_autohide|lease_set|new_variant)"),
            Arg("--limit", type=int, default=50, help="Max rows (default 50)"),
        ),
        aliases=("selfheal-audit",),
        help="Show the self-heal audit log"),
    Cmd("selfheal", "set-status", _cmd_selfheal_set_status,
        args=(
            Arg("id", type=int, help="error_events.id"),
            Arg("status",
                help="open|diagnosing|awaiting_approval|non_issue_proposed|non_issue|resolved"),
            Arg("--action-item-id", type=int, dest="action_item_id", default=None),
        ),
        aliases=("selfheal-set-status",),
        help="Update error_event status"),
    Cmd("selfheal", "reset", _cmd_selfheal_reset_diagnosing,
        args=(Arg("id", type=int, help="error_events.id"),),
        aliases=("selfheal-reset-diagnosing",),
        help="Reset stuck diagnosing->open"),
    Cmd("selfheal", "propose", _cmd_selfheal_propose_nonissue,
        args=(Arg("id", type=int, help="error_events.id"),),
        aliases=("selfheal-propose-nonissue",),
        help="Mark an error_event as non_issue_proposed (visible benign proposal)"),
)
