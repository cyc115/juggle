"""P9 R3/R4: the LIVE CLI's flat commands match the COMMANDS table exactly.

Originally (R3) this compared the COMMANDS table against the four hand-written
register() walls. R4 deleted those walls and wired main() to build_parser(COMMANDS)
via juggle_cli.build_cli_parser(). The same fidelity invariant now reads through
the new seam: every flat command the LIVE CLI parser exposes must match the
COMMANDS table's leaf signature + handler, and the group/entry-verb registration
must not shadow or alter any of the 51 ported commands.

Strategy: build the live CLI parser (build_cli_parser) and the COMMANDS-only parser
(build_parser), and compare leaf signatures (positionals + option-string→dest) for
every ported command.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import build_cli_parser  # noqa: E402
from juggle_cli_commands import COMMANDS  # noqa: E402
from juggle_cli_spec import build_parser  # noqa: E402

# The authoritative set of flat subcommands the 4 walls register (§1.1-§1.4).
# project/graph/project-graph/runs/autopilot are already grouped (NOT walls) and
# the entry-module verbs (verify/vault-path/...) live in juggle_cli.py — neither
# is in R3 scope.
PORTED = {
    # threads (§1.1)
    "start", "stop", "doctor", "create-thread", "switch-thread", "update-meta",
    "close-thread", "show-topics", "get-archive-candidates", "archive-thread",
    "unarchive-thread", "set-summarized-count", "get-stale-threads", "get-messages",
    # agents (§1.2)
    "complete-agent", "fail-agent", "integrate", "request-action", "notify",
    "ack-action", "list-actions", "check-agents", "spawn-agent", "list-agents",
    "get-agent", "release-agent", "decommission-agent", "send-task",
    "send-message", "set-watchdog", "stop-watchdog",
    # misc (§1.3)
    "get-context", "init-db", "agent-tools", "grep-vault", "retain", "digest",
    "next-action", "research", "schedule-dogfood", "schedule-autofix",
    "schedule-reflect", "cockpit", "add-node", "db-flush",
    # selfheal (§1.4)
    "list-selfheal", "show-selfheal", "selfheal-audit", "selfheal-set-status",
    "selfheal-reset-diagnosing", "selfheal-propose-nonissue",
}

def _live_choices():
    """The live CLI parser's subcommand leaves (build_parser flat commands + the
    out-of-scope groups + entry verbs, all wired by build_cli_parser)."""
    parser = build_cli_parser(vault_path_default="/tmp/vault")
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("no subparsers on the live CLI parser")


def _commands_choices():
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("build_parser produced no subparsers")


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


# ── completeness ──────────────────────────────────────────────────────────────


def test_commands_covers_exactly_the_ported_walls():
    verbs = {c.verb for c in COMMANDS}
    assert verbs == PORTED, (
        f"missing={PORTED - verbs} unexpected={verbs - PORTED}"
    )


def test_all_ported_are_flat_top_level_no_rename_yet():
    # R3 keeps legacy flat names as the canonical verb → resource is None.
    for c in COMMANDS:
        assert c.resource is None, f"{c.verb} should stay flat until G1"


# ── per-leaf arg-signature fidelity (catches every transcription error) ────────


def test_every_ported_leaf_matches_the_live_cli():
    live = _live_choices()
    cmds = _commands_choices()
    for name in sorted(PORTED):
        assert name in cmds, f"{name} absent from build_parser(COMMANDS)"
        assert name in live, f"{name} absent from the live CLI parser"
        assert _sig(cmds[name]) == _sig(live[name]), (
            f"arg signature mismatch for {name!r}:\n"
            f"  COMMANDS={_sig(cmds[name])}\n  live    ={_sig(live[name])}"
        )


# ── handler identity (the live CLI binds the COMMANDS handler, not shadowed) ───


def test_live_cli_handlers_are_the_commands_handlers():
    live = _live_choices()
    cmds = _commands_choices()
    for name in sorted(PORTED):
        assert live[name].get_default("func") is cmds[name].get_default("func"), (
            f"{name}: live CLI handler differs from the COMMANDS table"
        )
