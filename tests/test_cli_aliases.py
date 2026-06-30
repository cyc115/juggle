"""P9 A1-alias-shim: the ALIASES map + rewrite_argv (silent by default).

Formalizes the G1/G2 pre-parse shim (spec §4): a module-level ALIASES dict derived
from COMMANDS.aliases (+ the entry-verb / project-graph aliases) and rewrite_argv
gaining a ``warn`` flag (default False = silent; D1 flips the call site to True).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli_aliases import ALIASES, legacy_alias_map, rewrite_argv  # noqa: E402
from juggle_cli_commands import COMMANDS  # noqa: E402

_LEGACY_NAMES = (
    Path(__file__).parent / "data" / "legacy_names.txt"
).read_text().split()


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


# ── A2: legacy-name coverage + `aliases --json` command ───────────────────────


def test_legacy_names_file_is_covered_by_aliases():
    # tests/data/legacy_names.txt is the authoritative legacy-name list; every
    # entry MUST stay aliased (a dropped alias = broken back-compat → fails here).
    assert _LEGACY_NAMES, "legacy_names.txt is empty"
    missing = set(_LEGACY_NAMES) - set(ALIASES)
    assert not missing, f"legacy names missing from ALIASES: {sorted(missing)}"


def test_aliases_json_command_dumps_the_full_map(capsys):
    from juggle_cli_aliases import cmd_aliases

    cmd_aliases(SimpleNamespace(json_out=True))
    out = capsys.readouterr().out
    dumped = json.loads(out)  # valid JSON object on stdout
    assert set(_LEGACY_NAMES) <= set(dumped)
    assert dumped["complete-agent"] == ["agent", "complete"]
    assert dumped["project-graph"] == ["graph"]


def test_aliases_command_registered_in_live_cli():
    from juggle_cli import build_cli_parser

    ns = build_cli_parser().parse_args(["aliases", "--json"])
    assert callable(ns.func)
    assert ns.json_out is True
