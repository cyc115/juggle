"""P9 R4/G1: main() builds its parser from build_parser(COMMANDS) + groups.

R4 wired build_cli_parser() (build_parser for the flat COMMANDS + the out-of-scope
groups + entry verbs). G1 renamed the flat commands to the uniform resource-verb
grammar (thread create, agent complete, …) and folded the vault/file entry verbs
into groups; legacy flat names keep resolving via the pre-parse alias shim
(_rewrite_legacy_argv). These pins assert the live parser exposes the new grammar,
the kept top-level verbs, and that the shim maps legacy → canonical.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import _rewrite_legacy_argv, build_cli_parser  # noqa: E402


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers on parser")


def test_exposes_resource_groups_and_kept_top_level_verbs():
    names = set(_subparsers(build_cli_parser()).choices)
    # new resource groups (G1)
    assert {"thread", "agent", "action", "watchdog", "selfheal", "db", "vault",
            "memory", "context", "node", "schedule", "research", "file"} <= names
    # top-level global verbs kept flat (§2.1)
    assert {"start", "stop", "doctor", "cockpit", "integrate"} <= names
    # out-of-scope groups still registered + verify entry verb
    assert {"project", "graph", "runs", "autopilot", "verify"} <= names
    # G2: project-graph folded into the graph group — no longer top-level
    assert "project-graph" not in names


def test_graph_load_folded_into_graph_group():
    # G2: `project-graph load` is now `graph load` (same handler + args).
    parser = build_cli_parser()
    ns = parser.parse_args(["graph", "load", "spec.md", "--project", "P9"])
    assert callable(ns.func)
    assert ns.file == "spec.md" and ns.project == "P9"


def test_project_graph_legacy_rewrites_to_graph_via_shim():
    assert _rewrite_legacy_argv(
        ["j", "project-graph", "load", "spec.md", "--project", "P9"]
    ) == ["j", "graph", "load", "spec.md", "--project", "P9"]


def test_resource_verb_parses_and_binds_handler():
    parser = build_cli_parser()
    ns = parser.parse_args(["agent", "complete", "T", "summary"])
    assert callable(ns.func)
    assert ns.thread_id == "T" and ns.result_summary == "summary"


def test_vault_grep_default_is_reinjected_under_group():
    # grep-vault → `vault grep`; the runtime --vault-path default must still inject.
    ns = build_cli_parser().parse_args(["vault", "grep", "term"])
    assert ns.vault_path, "vault grep --vault-path default not re-injected"


def test_vault_path_and_file_open_folded_into_groups():
    parser = build_cli_parser()
    assert callable(parser.parse_args(["vault", "path"]).func)
    assert parser.parse_args(["file", "open", "/tmp/x"]).file == "/tmp/x"


def test_project_group_still_works():
    ns = build_cli_parser().parse_args(["project", "list"])
    assert callable(ns.func)


def test_doctor_handler_is_the_exit_propagating_wrapper():
    ns = build_cli_parser().parse_args(["doctor", "--dry-run"])
    assert ns.func.__name__ == "_doctor_dispatch"


def test_unknown_command_errors():
    import pytest
    with pytest.raises(SystemExit):
        build_cli_parser().parse_args(["no-such-command"])


# ── legacy alias shim (G1 transitional; A1 formalizes) ────────────────────────


def test_rewrite_legacy_argv_maps_flat_names_to_resource_verb():
    assert _rewrite_legacy_argv(["j", "complete-agent", "T", "s"]) == [
        "j", "agent", "complete", "T", "s"]
    assert _rewrite_legacy_argv(["j", "create-thread", "x"]) == [
        "j", "thread", "create", "x"]
    assert _rewrite_legacy_argv(["j", "db-flush", "--status"]) == [
        "j", "db", "flush", "--status"]
    # entry-verb aliases
    assert _rewrite_legacy_argv(["j", "vault-path"]) == ["j", "vault", "path"]
    assert _rewrite_legacy_argv(["j", "open-in-editor", "/f"]) == [
        "j", "file", "open", "/f"]


def test_rewrite_legacy_argv_leaves_new_and_flat_verbs_untouched():
    assert _rewrite_legacy_argv(["j", "agent", "complete"]) == ["j", "agent", "complete"]
    assert _rewrite_legacy_argv(["j", "doctor", "--dry-run"]) == ["j", "doctor", "--dry-run"]
    assert _rewrite_legacy_argv(["j"]) == ["j"]


def test_legacy_name_resolves_through_the_shim_end_to_end():
    # The shim output parses cleanly against the live parser to the right handler.
    parser = build_cli_parser()
    rewritten = _rewrite_legacy_argv(["j", "complete-agent", "T", "summary"])[1:]
    ns = parser.parse_args(rewritten)
    assert ns.thread_id == "T" and ns.result_summary == "summary"
