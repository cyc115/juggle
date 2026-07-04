"""juggle_cli_parsers_loop — argparse wiring for the `loop <verb>` group
(loop-entity V1, Phase 4).

V1 registers only `loop create` (the transactional single-topic create the
schedule:create router calls). `loop list`/`loop delete` are V2.
Must not own: handler logic (lives in juggle_cmd_loop_create).
"""
from __future__ import annotations

from juggle_cmd_loop_create import cmd_loop_create


def register_loop_parsers(subparsers) -> None:
    """Register the `loop <verb>` subcommand group on ``subparsers``."""
    p_loop = subparsers.add_parser("loop", help="Manage recurring loops")
    _ls = p_loop.add_subparsers(dest="loop_command", required=True)

    _p = _ls.add_parser("create", help="Create a single-topic loop (transactional)")
    _p.add_argument("--template", required=True,
                    help="Path to the validated single-topic template JSON")
    _p.add_argument("--cadence", required=True,
                    help="Cadence, e.g. 'every 15m' / 'daily at 09:00'")
    _p.add_argument("--name", help="Loop project name (default 'loop <L#>')")
    _p.add_argument("--objective", default="", help="Loop project objective")
    _p.set_defaults(func=cmd_loop_create)
