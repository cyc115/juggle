"""P9 A1-alias-shim: the ALIASES map + rewrite_argv (silent by default).

Formalizes the G1/G2 pre-parse shim (spec §4): a module-level ALIASES dict derived
from COMMANDS.aliases (+ the entry-verb / project-graph aliases) and rewrite_argv
gaining a ``warn`` flag (default False = silent; D1 flips the call site to True).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli_aliases import ALIASES, legacy_alias_map, rewrite_argv  # noqa: E402
from juggle_cli_commands import COMMANDS  # noqa: E402


# ── ALIASES is derived from COMMANDS.aliases ──────────────────────────────────


def test_aliases_is_a_dict_materializing_the_legacy_map():
    assert isinstance(ALIASES, dict)
    assert ALIASES == legacy_alias_map()


def test_every_commands_alias_maps_to_its_canonical_resource_verb():
    for c in COMMANDS:
        target = [c.verb] if c.resource is None else [c.resource, c.verb]
        for alias in c.aliases:
            assert ALIASES[alias] == target, alias


def test_entry_verb_and_project_graph_aliases_present():
    assert ALIASES["vault-path"] == ["vault", "path"]
    assert ALIASES["vault-name"] == ["vault", "name"]
    assert ALIASES["open-in-editor"] == ["file", "open"]
    assert ALIASES["project-graph"] == ["graph"]


# ── rewrite_argv mechanics ────────────────────────────────────────────────────


def test_rewrite_maps_legacy_flat_names():
    assert rewrite_argv(["j", "complete-agent", "T", "s"]) == ["j", "agent", "complete", "T", "s"]
    assert rewrite_argv(["j", "create-thread", "x"]) == ["j", "thread", "create", "x"]
    assert rewrite_argv(["j", "db-flush", "--status"]) == ["j", "db", "flush", "--status"]
    assert rewrite_argv(["j", "project-graph", "load", "f", "--project", "P"]) == [
        "j", "graph", "load", "f", "--project", "P"]


def test_rewrite_leaves_canonical_and_flat_untouched():
    assert rewrite_argv(["j", "agent", "complete"]) == ["j", "agent", "complete"]
    assert rewrite_argv(["j", "doctor", "--dry-run"]) == ["j", "doctor", "--dry-run"]
    assert rewrite_argv(["j"]) == ["j"]
    assert rewrite_argv(["j", "--help"]) == ["j", "--help"]


def test_rewrite_collision_guard_research_run_not_double_rewritten():
    # `research` is both the alias for `research run` AND the resource group.
    assert rewrite_argv(["j", "research", "run", "topic"]) == ["j", "research", "run", "topic"]
    assert rewrite_argv(["j", "research", "topic"]) == ["j", "research", "run", "topic"]


# ── warn flag: silent by default (A1), stderr when on (for D1) ────────────────


def test_rewrite_is_silent_by_default(capsys):
    rewrite_argv(["j", "complete-agent", "T", "s"])
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_rewrite_warns_to_stderr_when_warn_true(capsys):
    out = rewrite_argv(["j", "complete-agent", "T", "s"], warn=True)
    assert out == ["j", "agent", "complete", "T", "s"]  # rewrite still happens
    captured = capsys.readouterr()
    assert captured.out == ""  # never stdout (agents parse stdout/JSON)
    assert "deprecated" in captured.err
    assert "agent complete" in captured.err


def test_rewrite_warn_true_silent_for_non_legacy(capsys):
    rewrite_argv(["j", "agent", "complete"], warn=True)
    assert capsys.readouterr().err == ""
