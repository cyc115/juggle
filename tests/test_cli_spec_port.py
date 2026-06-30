"""P9 R3-port-threads: COMMANDS faithfully mirrors the 4 hand-written walls.

The COMMANDS table (juggle_cli_spec) must reproduce EXACTLY the flat subcommands
the four register() walls (threads/agents/misc/selfheal) expose today — same verb
names (legacy flat, no rename yet), same per-leaf arg signature, same handler
objects. build_parser(COMMANDS) is still parallel + unused (R4 wires it), so these
tests are the only thing pinning the port's fidelity.

Strategy: build the REAL parser by calling the wall register() functions, build the
COMMANDS parser via build_parser(), and compare leaf signatures (positionals +
option-string→dest) for every ported command.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_cli_parsers_agents as wa  # noqa: E402
import juggle_cli_parsers_misc as wm  # noqa: E402
import juggle_cli_parsers_threads as wt  # noqa: E402
from juggle_cli_spec import COMMANDS, build_parser  # noqa: E402

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

# Wall handlers that are inline lambdas (no importable function), so the COMMANDS
# entry uses an equivalent named wrapper — identity won't match (signature does).
_WRAPPED = {"integrate", "schedule-dogfood", "schedule-autofix", "schedule-reflect"}


def _real_choices():
    parser = argparse.ArgumentParser(prog="juggle")
    sub = parser.add_subparsers(dest="command")
    wt.register(sub)
    wa.register(sub)
    wm.register(sub, vault_path_default="/tmp/vault")
    return sub.choices


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


def test_every_ported_leaf_signature_matches_the_real_wall():
    real = _real_choices()
    cmds = _commands_choices()
    for name in sorted(PORTED):
        assert name in cmds, f"{name} absent from build_parser(COMMANDS)"
        assert name in real, f"{name} absent from real wall parser"
        assert _sig(cmds[name]) == _sig(real[name]), (
            f"arg signature mismatch for {name!r}:\n"
            f"  COMMANDS={_sig(cmds[name])}\n  real    ={_sig(real[name])}"
        )


# ── handler identity (named handlers must be the SAME object as the walls) ─────


def test_named_handlers_are_identical_to_the_walls():
    real = _real_choices()
    cmds = _commands_choices()
    for name in sorted(PORTED - _WRAPPED):
        assert cmds[name].get_default("func") is real[name].get_default("func"), (
            f"{name}: handler differs from the wall"
        )
