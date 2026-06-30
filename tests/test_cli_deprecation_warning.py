"""P9 D1-warn-on: main() routes legacy names with warn=True (stderr only).

Spec §5 stage (b): invoking a legacy flat command still works (zero-breakage) but
now prints a one-line deprecation notice to STDERR. stdout + exit code are
unchanged from the new resource-verb form (the A3 parity test pins stdout; this
pins the stderr half + that the notice never leaks to stdout).

Driven through the real entry point (juggle_cli.main()) with a monkeypatched argv,
using `vault-path` — a read-only, DB-free legacy command (→ `vault path`). The
`_JUGGLE_TEST_DB` env silences main()'s unrelated watchdog-not-running stderr line,
isolating stderr to the deprecation notice.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_cli  # noqa: E402


def test_legacy_name_warns_to_stderr_only(monkeypatch, capsys):
    monkeypatch.setenv("_JUGGLE_TEST_DB", "1")
    monkeypatch.setattr(sys, "argv", ["juggle", "vault-path"])

    juggle_cli.main()  # no SystemExit on the success path

    out, err = capsys.readouterr()
    # deprecation notice on STDERR, naming the new form
    assert "deprecated" in err
    assert "vault path" in err
    # stdout is the command's real output (the vault path) — NEVER the warning
    assert out.strip()
    assert "deprecated" not in out


def test_new_form_emits_no_deprecation(monkeypatch, capsys):
    monkeypatch.setenv("_JUGGLE_TEST_DB", "1")
    monkeypatch.setattr(sys, "argv", ["juggle", "vault", "path"])

    juggle_cli.main()

    out, err = capsys.readouterr()
    assert "deprecated" not in err
    assert out.strip()


def test_legacy_and_new_stdout_identical_under_warn(monkeypatch, capsys):
    # The deprecation warning (stderr) must not perturb stdout: byte-identical.
    monkeypatch.setenv("_JUGGLE_TEST_DB", "1")

    monkeypatch.setattr(sys, "argv", ["juggle", "vault-path"])
    juggle_cli.main()
    legacy_out = capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["juggle", "vault", "path"])
    juggle_cli.main()
    new_out = capsys.readouterr().out

    assert legacy_out == new_out
