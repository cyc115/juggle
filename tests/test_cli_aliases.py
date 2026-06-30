"""P9 X2-remove-aliases: the legacy flat-name alias layer is GONE.

These pins formerly (A1/A2) asserted the legacy alias map was DERIVED from
COMMANDS.aliases and that legacy flat names rewrote to the new resource-verb form.
X2 (2026-06-30, user-approved IRREVERSIBLE removal — spec §5 stage d) deletes that
layer: ``ALIASES`` is now empty, ``rewrite_argv`` is an inert pass-through, and every
legacy flat name is rejected by the live parser (exit 2). Per the X2 contract the
coverage is REWRITTEN to assert rejection — not deleted — so the authoritative
legacy-name list (tests/data/legacy_names.txt) still gates the behavior.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import build_cli_parser  # noqa: E402
from juggle_cli_aliases import ALIASES, rewrite_argv  # noqa: E402

_LEGACY_NAMES = (
    Path(__file__).parent / "data" / "legacy_names.txt"
).read_text().split()


# ── ALIASES is now empty (X2 removal) ─────────────────────────────────────────


def test_aliases_map_is_empty_after_removal():
    assert ALIASES == {}


def test_no_legacy_name_remains_in_aliases():
    # The authoritative legacy list must share NOTHING with the (now empty) map.
    assert _LEGACY_NAMES, "legacy_names.txt is empty"
    assert set(_LEGACY_NAMES) & set(ALIASES) == set()


# ── rewrite_argv is now an inert pass-through ─────────────────────────────────


def test_rewrite_no_longer_maps_legacy_flat_names():
    # Legacy tokens ride through unchanged — there is no alias to splice anymore.
    assert rewrite_argv(["j", "complete-agent", "T", "s"]) == ["j", "complete-agent", "T", "s"]
    assert rewrite_argv(["j", "create-thread", "x"]) == ["j", "create-thread", "x"]
    assert rewrite_argv(["j", "db-flush", "--status"]) == ["j", "db-flush", "--status"]
    assert rewrite_argv(["j", "project-graph", "load", "f"]) == ["j", "project-graph", "load", "f"]


def test_rewrite_leaves_canonical_and_flat_untouched():
    assert rewrite_argv(["j", "agent", "complete"]) == ["j", "agent", "complete"]
    assert rewrite_argv(["j", "doctor", "--dry-run"]) == ["j", "doctor", "--dry-run"]
    assert rewrite_argv(["j"]) == ["j"]
    assert rewrite_argv(["j", "--help"]) == ["j", "--help"]


def test_rewrite_is_always_silent_now(capsys):
    # No alias resolves, so even warn=True emits nothing (the warn branch is dead).
    rewrite_argv(["j", "complete-agent", "T", "s"], warn=True)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# ── every legacy flat name is REJECTED by the live parser (exit 2) ────────────


@pytest.mark.parametrize("name", _LEGACY_NAMES)
def test_legacy_flat_name_is_rejected_by_parser(name):
    # Either an unknown top-level choice, or (for `research`, which survives as a
    # resource group) a missing required subcommand — both are argparse exit 2.
    parser = build_cli_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([name])
    assert exc.value.code == 2


# ── `aliases --json` still registered; now emits {} ───────────────────────────


def test_aliases_json_command_dumps_empty_map(capsys):
    from juggle_cli_aliases import cmd_aliases

    cmd_aliases(SimpleNamespace(json_out=True))
    out = capsys.readouterr().out
    assert json.loads(out) == {}


def test_aliases_command_registered_in_live_cli():
    ns = build_cli_parser().parse_args(["aliases", "--json"])
    assert callable(ns.func)
    assert ns.json_out is True
