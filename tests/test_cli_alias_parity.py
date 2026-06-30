"""P9 X2-remove-aliases: legacy flat names no longer resolve — they are rejected.

This pin was formerly A3-output-parity (spec §5 stage b): it asserted a legacy flat
name's stdout was byte-identical to its new resource-verb form, because the silent
alias shim rewrote one to the other. X2 (2026-06-30, user-approved IRREVERSIBLE
removal — spec §5 stage d) deletes that shim. There is no parity to assert anymore;
instead we pin the removal — invoking a legacy name through the same main()-style
path (shim → parse) now exits 2 (argparse unknown choice), while the new form still
works. The new forms are hard-coded so a regression that re-introduces the alias
layer is caught here.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import build_cli_parser  # noqa: E402
from juggle_cli_aliases import rewrite_argv  # noqa: E402

# (legacy argv tail, the new resource-verb form it USED to alias to). Read-only,
# no-positional commands whose new form parses cleanly.
PARITY_PAIRS = [
    (["list-actions"], ["action", "list"]),
    (["show-topics"], ["thread", "list"]),
    (["list-selfheal"], ["selfheal", "list"]),
    (["check-agents"], ["agent", "check"]),
    (["vault-path"], ["vault", "path"]),
    (["vault-name"], ["vault", "name"]),
]


def _parse_as_main(argv_tail):
    """Apply the shim exactly as main() does, then parse against the live parser."""
    parser = build_cli_parser()
    rewritten = rewrite_argv(["juggle", *argv_tail])[1:]
    return parser.parse_args(rewritten)


@pytest.mark.parametrize("legacy,new", PARITY_PAIRS)
def test_legacy_name_now_rejected_new_form_still_works(legacy, new):
    # The legacy flat name is no longer rewritten → argparse rejects it (exit 2).
    with pytest.raises(SystemExit) as exc:
        _parse_as_main(legacy)
    assert exc.value.code == 2
    # …while the canonical resource-verb form parses to a real handler.
    ns = _parse_as_main(new)
    assert callable(ns.func)
