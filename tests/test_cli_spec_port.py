"""P9 R3→G1: the LIVE CLI resolves every COMMANDS entry to its handler.

R3 ported the 4 walls into COMMANDS; R4 wired build_cli_parser(); G1 renamed the
flat names to the uniform resource-verb grammar (Cmd.resource/verb) and recorded
the legacy flat name in Cmd.aliases. The fidelity invariant now: for EVERY Cmd in
the table, the live CLI parser navigates [resource, verb] (or [verb] for a flat
top-level verb) to that exact handler with a matching arg signature, and the
group/entry-verb registration does not shadow it.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import build_cli_parser  # noqa: E402
from juggle_cli_commands import COMMANDS  # noqa: E402
from juggle_cli_spec import build_parser  # noqa: E402


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers on parser")


def _leaf_for(parser, cmd):
    """Navigate the parser to ``cmd``'s leaf parser (root→resource→verb)."""
    root = _subparsers(parser)
    if cmd.resource is None:
        return root.choices.get(cmd.verb)
    group = root.choices.get(cmd.resource)
    if group is None:
        return None
    for action in group._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices.get(cmd.verb)
    return None


def _sig(parser):
    """(positional dests in order, sorted option-string→dest pairs), minus -h."""
    positionals: list[str] = []
    options: dict[str, str] = {}
    for action in parser._actions:
        if action.dest == "help":
            continue
        if action.option_strings:
            for opt in action.option_strings:
                options[opt] = action.dest
        else:
            positionals.append(action.dest)
    return tuple(positionals), tuple(sorted(options.items()))


def test_every_command_resolves_in_the_live_cli_to_its_handler():
    live = build_cli_parser(vault_path_default="/tmp/vault")
    for c in COMMANDS:
        leaf = _leaf_for(live, c)
        label = f"{c.resource or ''} {c.verb}".strip()
        assert leaf is not None, f"{label!r} not resolvable in the live CLI"
        assert leaf.get_default("func") is c.handler, f"{label!r} handler differs"


def test_live_leaf_signature_matches_the_commands_table():
    live = build_cli_parser(vault_path_default="/tmp/vault")
    cmds = build_parser()  # COMMANDS-only parser (same source)
    for c in COMMANDS:
        live_leaf = _leaf_for(live, c)
        cmds_leaf = _leaf_for(cmds, c)
        label = f"{c.resource or ''} {c.verb}".strip()
        assert _sig(live_leaf) == _sig(cmds_leaf), f"signature mismatch for {label!r}"


def test_canonical_names_only_legacy_aliases_not_registered():
    # The grammar tree is canonical-only; legacy flat names resolve via the A1/G1
    # pre-parse shim, NOT as parser choices. EXCEPTION: a legacy name that equals a
    # canonical resource group name (`research` is both the `research run` alias and
    # the resource) legitimately appears as the group — the shim's already-canonical
    # guard prevents double-rewrite.
    names = set(_subparsers(build_cli_parser()).choices)
    canonical_resources = {c.resource for c in COMMANDS if c.resource}
    legacy = {a for c in COMMANDS for a in c.aliases} - canonical_resources
    assert not (legacy & names), f"legacy names leaked into the parser tree: {legacy & names}"


def test_every_command_has_a_resource_or_is_a_kept_flat_verb():
    KEPT_FLAT = {"start", "stop", "doctor", "cockpit", "integrate", "metrics"}
    for c in COMMANDS:
        if c.resource is None:
            assert c.verb in KEPT_FLAT, f"unexpected flat verb {c.verb!r}"
        else:
            assert c.aliases, f"{c.resource} {c.verb} should carry its legacy alias"
