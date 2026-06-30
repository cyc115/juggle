"""P9 A3-output-parity: a legacy command's stdout is byte-identical to its new form.

Spec §5 stage (b) gate. Typing a legacy flat name (which main() routes through the
alias shim, rewrite_argv) must produce the EXACT same stdout as typing the new
resource-verb form directly — otherwise the silent alias layer would be a subtle
behavior change. The new forms are hard-coded (not derived from ALIASES) so a
mis-mapping in the shim is caught, not masked.

In-process: build the live parser, apply the shim exactly as main() does, dispatch,
and capture stdout. conftest gives each test a throwaway JUGGLE_DB_PATH, so the
read-only list commands below emit deterministic (empty) output, identical between
the two runs against the same fresh DB.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli import build_cli_parser  # noqa: E402
from juggle_cli_aliases import ALIASES, rewrite_argv  # noqa: E402

# (legacy argv tail, intended new-form argv tail). Read-only, no-positional
# commands whose stdout is deterministic on a fresh isolated DB / from config.
PARITY_PAIRS = [
    (["list-actions"], ["action", "list"]),
    (["show-topics"], ["thread", "list"]),
    (["list-selfheal"], ["selfheal", "list"]),
    (["check-agents"], ["agent", "check"]),
    (["vault-path"], ["vault", "path"]),
    (["vault-name"], ["vault", "name"]),
]


def _stdout_for(argv_tail):
    """Run a command exactly as main() would (shim → parse → dispatch), capturing
    only its stdout."""
    parser = build_cli_parser()
    rewritten = rewrite_argv(["juggle", *argv_tail])[1:]
    args = parser.parse_args(rewritten)
    buf = io.StringIO()
    with redirect_stdout(buf):
        args.func(args)
    return buf.getvalue()


@pytest.mark.parametrize("legacy,new", PARITY_PAIRS)
def test_legacy_stdout_is_byte_identical_to_new_form(legacy, new):
    # sanity: the pair is genuinely a rename (legacy != new) and the shim agrees.
    assert legacy != new
    assert ALIASES[legacy[0]] == new, f"{legacy[0]} maps to {ALIASES[legacy[0]]}, not {new}"
    assert _stdout_for(legacy) == _stdout_for(new), (
        f"stdout parity broken for {legacy[0]!r} vs {' '.join(new)!r}"
    )
