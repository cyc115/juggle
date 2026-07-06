"""juggle_cli_parsers_groups — one call that registers every noun-verb resource
group onto the root subparsers.

Extracted from juggle_cli.build_cli_parser (2026-07-05 LOC-gate pre-work for the
`vcs` group): juggle_cli.py sat at the 300-line budget, so the block of
``register_*_parsers(subparsers)`` calls (already-grouped, out-of-COMMANDS
families) moved here behind a single ``register_resource_groups`` entry point.
New resource groups (e.g. ``vcs``) register HERE, never by growing juggle_cli.py.

Must not own: the declarative flat COMMANDS table (juggle_cli_spec.build_parser)
or the entry-module verbs (open/vault-path/verify) that stay in juggle_cli.py.
"""
from __future__ import annotations

import juggle_cmd_autopilot
from juggle_cmd_graph import register_graph_parsers
from juggle_cmd_runs import register_runs_parsers
from juggle_cmd_spool import register_spool_parsers
from juggle_cli_parsers_project import register_project_parsers
from juggle_cli_parsers_loop import register_loop_parsers
from juggle_cli_parsers_vcs import register_vcs_parsers


def register_resource_groups(subparsers) -> None:
    """Register every noun-verb resource group onto ``subparsers``.

    These are the families already in canonical ``<noun> <verb>`` shape and not
    ported into the declarative COMMANDS table."""
    register_graph_parsers(subparsers)
    register_runs_parsers(subparsers)
    register_spool_parsers(subparsers)
    register_project_parsers(subparsers)
    register_loop_parsers(subparsers)
    register_vcs_parsers(subparsers)
    juggle_cmd_autopilot.register(subparsers)
