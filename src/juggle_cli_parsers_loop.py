"""juggle_cli_parsers_loop — argparse wiring for the `loop <verb>` group
(loop-entity V2, Phase 4a).

Registers `loop create` (the transactional multi-topic create the schedule:create
router calls) and `loop plan` (the read-only pre-create confirm-card, §6.3).
`loop list`/`loop delete` land with the schedule namespace work.
Must not own: handler logic (lives in juggle_cmd_loop_create).
"""
from __future__ import annotations

from juggle_cmd_loop_create import cmd_loop_create, cmd_loop_plan


def register_loop_parsers(subparsers) -> None:
    """Register the `loop <verb>` subcommand group on ``subparsers``."""
    p_loop = subparsers.add_parser("loop", help="Manage recurring loops")
    _ls = p_loop.add_subparsers(dest="loop_command", required=True)

    _p = _ls.add_parser("create", help="Create a loop from a topic-DAG template (transactional)")
    _p.add_argument("--template", required=True,
                    help="Path to the validated loop template JSON")
    _p.add_argument("--cadence", required=True,
                    help="Cadence, e.g. 'every 15m' / 'daily at 09:00'")
    _p.add_argument("--name", help="Loop project name (default 'loop <L#>')")
    _p.add_argument("--objective", default="", help="Loop project objective")
    _p.set_defaults(func=cmd_loop_create)

    _pp = _ls.add_parser("plan",
                         help="Preview the decomposed topic-DAG confirm-card (read-only)")
    _pp.add_argument("--template", required=True,
                     help="Path to the loop template JSON")
    _pp.add_argument("--cadence", required=True,
                     help="Cadence, e.g. 'every 15m' / 'daily at 09:00'")
    _pp.set_defaults(func=cmd_loop_plan)
