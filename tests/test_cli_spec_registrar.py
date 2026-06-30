"""P9 R2-generic-registrar: build_parser() driven by the COMMANDS spec table.

Pins the generic registrar that turns a tuple of declarative Cmd entries into an
argparse parser with resource subparser groups + top-level global verbs. This is
PARALLEL to the four hand-written register() walls and NOT wired into main() yet,
so the tests drive build_parser() with synthetic Cmd lists (dummy handlers) rather
than the real CLI.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli_commands import COMMANDS  # noqa: E402
from juggle_cli_spec import Arg, Cmd, build_parser  # noqa: E402


# Distinct sentinel handlers so set_defaults(func=...) wiring is verifiable.
def _h_start(args):
    return "start"


def _h_create(args):
    return "create"


def _h_list(args):
    return "list"


def _h_agent_list(args):
    return "agent_list"


# ── top-level global verbs (resource is None) ─────────────────────────────────


def test_global_verb_parses_and_binds_handler():
    parser = build_parser([Cmd(None, "start", _h_start, help="Start juggle")])
    ns = parser.parse_args(["start"])
    assert ns.command == "start"
    assert ns.func is _h_start


# ── resource subparser groups ─────────────────────────────────────────────────


def test_resource_verb_groups_and_args():
    parser = build_parser([
        Cmd("thread", "create", _h_create, args=(Arg("topic"),),
            help="Create a topic thread"),
        Cmd("thread", "list", _h_list, help="List topics"),
    ])
    ns = parser.parse_args(["thread", "create", "hello"])
    assert ns.func is _h_create
    assert ns.topic == "hello"
    assert ns.thread_command == "create"

    ns2 = parser.parse_args(["thread", "list"])
    assert ns2.func is _h_list


def test_two_verbs_share_one_resource_group_without_conflict():
    # Building two verbs under the same resource must reuse the group, not raise.
    parser = build_parser([
        Cmd("thread", "create", _h_create),
        Cmd("thread", "list", _h_list),
    ])
    assert parser.parse_args(["thread", "create"]).func is _h_create
    assert parser.parse_args(["thread", "list"]).func is _h_list


def test_distinct_resources_do_not_collide():
    parser = build_parser([
        Cmd("thread", "list", _h_list),
        Cmd("agent", "list", _h_agent_list),
    ])
    assert parser.parse_args(["thread", "list"]).func is _h_list
    assert parser.parse_args(["agent", "list"]).func is _h_agent_list


def test_flag_args_are_wired():
    parser = build_parser([
        Cmd("agent", "list", _h_agent_list,
            args=(Arg("--json", dest="json_out", action="store_true"),)),
    ])
    assert parser.parse_args(["agent", "list"]).json_out is False
    assert parser.parse_args(["agent", "list", "--json"]).json_out is True


# ── required-subcommand enforcement ───────────────────────────────────────────


def test_missing_command_errors():
    parser = build_parser([Cmd("thread", "list", _h_list)])
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_resource_without_verb_errors():
    parser = build_parser([Cmd("thread", "list", _h_list)])
    with pytest.raises(SystemExit):
        parser.parse_args(["thread"])


# ── aliases are NOT argparse aliases (handled by the A1 pre-parse shim) ────────


def test_build_parser_ignores_aliases():
    """Legacy aliases live in the A1 argv-rewrite shim, NOT the parser tree — the
    canonical verb parses; the legacy flat name is not a valid subcommand here."""
    parser = build_parser([
        Cmd("thread", "create", _h_create, aliases=("create-thread",)),
    ])
    assert parser.parse_args(["thread", "create"]).func is _h_create
    with pytest.raises(SystemExit):
        parser.parse_args(["create-thread"])


# ── default COMMANDS table exists (populated by R3) ───────────────────────────


def test_commands_table_is_a_tuple_and_build_parser_defaults_to_it():
    assert isinstance(COMMANDS, tuple)
    # build_parser() with no args uses the module COMMANDS table without raising
    # at construction time (empty/seed table is fine — R3 populates it).
    build_parser()
