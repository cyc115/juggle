"""P9 X2-remove-aliases: legacy names no longer warn — they are rejected (exit 2).

This pin was formerly D1-warn-on (spec §5 stage b): invoking a legacy flat command
still worked but emitted a one-line deprecation notice to STDERR. X2 (2026-06-30,
user-approved IRREVERSIBLE removal — spec §5 stage d) deletes the alias layer, so a
legacy name is now an unknown argparse choice: main() exits 2 (argparse prints usage
to stderr) and no command runs. The new resource-verb form still works silently.

Driven through the real entry point (juggle_cli.main()) with a monkeypatched argv,
using `vault-path` (legacy) vs `vault path` (new).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_cli  # noqa: E402


def test_legacy_name_is_rejected_with_exit_2(monkeypatch, capsys):
    monkeypatch.setenv("_JUGGLE_TEST_DB", "1")
    monkeypatch.setattr(sys, "argv", ["juggle", "vault-path"])

    with pytest.raises(SystemExit) as exc:
        juggle_cli.main()
    assert exc.value.code == 2

    out, err = capsys.readouterr()
    # argparse usage/error goes to stderr; the legacy command produced no stdout.
    assert "invalid choice" in err
    assert out.strip() == ""


def test_new_form_runs_without_deprecation(monkeypatch, capsys):
    monkeypatch.setenv("_JUGGLE_TEST_DB", "1")
    monkeypatch.setattr(sys, "argv", ["juggle", "vault", "path"])

    juggle_cli.main()  # no SystemExit on the success path

    out, err = capsys.readouterr()
    assert "deprecated" not in err
    assert out.strip()
