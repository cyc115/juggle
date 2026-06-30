"""P9 R4-switch-entrypoint: main() builds its parser from build_parser(COMMANDS).

The four hand-written register() walls are deleted; the 51 flat commands now come
from the declarative COMMANDS table via juggle_cli.build_cli_parser(), which also
registers the out-of-scope groups (project/graph/project-graph/runs/autopilot) and
the entry-module verbs (open-in-editor/vault-path/vault-name/verify), and re-injects
the runtime grep-vault --vault-path default. These pins assert the live parser still
exposes everything the walls did.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import build_cli_parser  # noqa: E402


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers on parser")


def test_exposes_all_command_families():
    names = set(_subparsers(build_cli_parser()).choices)
    # flat (ported) commands — a representative sample from each wall
    assert {"start", "create-thread", "doctor", "complete-agent", "integrate",
            "cockpit", "grep-vault", "add-node", "db-flush",
            "list-selfheal", "selfheal-audit"} <= names
    # out-of-scope groups still registered (not ported — R3/R4 keep imperative)
    assert {"project", "graph", "project-graph", "runs", "autopilot"} <= names
    # entry-module verbs
    assert {"open-in-editor", "vault-path", "vault-name", "verify"} <= names


def test_flat_command_parses_and_binds_handler():
    parser = build_cli_parser()
    ns = parser.parse_args(["complete-agent", "T", "summary"])
    assert callable(ns.func)
    assert ns.thread_id == "T" and ns.result_summary == "summary"


def test_grep_vault_default_is_reinjected():
    # COMMANDS carries --vault-path default=None; build_cli_parser must re-inject
    # the runtime-resolved vault root (cmd_grep_vault passes it straight to grep).
    ns = build_cli_parser().parse_args(["grep-vault", "term"])
    assert ns.vault_path, "grep-vault --vault-path default not re-injected"


def test_project_group_still_works():
    ns = build_cli_parser().parse_args(["project", "list"])
    assert callable(ns.func)


def test_doctor_handler_is_the_exit_propagating_wrapper():
    # doctor must keep its exit-code-propagating dispatcher (not bare cmd_doctor).
    ns = build_cli_parser().parse_args(["doctor", "--dry-run"])
    assert ns.func.__name__ == "_doctor_dispatch"


def test_unknown_command_errors():
    with __import__("pytest").raises(SystemExit):
        build_cli_parser().parse_args(["no-such-command"])
