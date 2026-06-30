"""P9 verify-harness smoke test.

Pins the harness that lets the 18-node P9 CLI-grammar-migration DAG load:
every node's `verify_cmd` is the single operator-free token `make p9-verify-<id>`,
backed by an executable wrapper in `scripts/p9_verify/<id>.sh` and the
`p9-verify-%` Makefile pattern rule.

Origin (2026-06-29): the `graph add-task` lint then FORBADE shell operators and
only allowlisted {make,uv,pytest,python,...} as the exe (`bash` not allowlisted),
so the §6 compound verify_cmds could not load verbatim — the `make p9-verify-<id>`
indirection was the fix and the harness it produced is still valid.

UPDATE (2026-06-30, user decision "full relax"): the lint now ACCEPTS shell
operators and any executable, so compound verify_cmds also load directly. The
make-indirection harness is no longer REQUIRED, but it remains correct and these
pins still hold (see test_bash_and_compound_forms_now_accepted_by_lint).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_upsert import lint_verify_cmd  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
P9_DIR = REPO / "scripts" / "p9_verify"

# The 18 §6 nodes (X2 is manual-gated but its wrapper still ships).
NODE_IDS = [
    "r1", "r2", "r3", "r4",
    "g1", "g2", "g3",
    "a1", "a2", "a3",
    "d1",
    "m1", "m2", "m3", "m4", "m5",
    "x1", "x2",
]


def test_all_18_wrappers_present_and_executable():
    found = sorted(p.stem for p in P9_DIR.glob("*.sh"))
    assert found == sorted(NODE_IDS), f"wrapper set mismatch: {found}"
    for nid in NODE_IDS:
        wrapper = P9_DIR / f"{nid}.sh"
        mode = wrapper.stat().st_mode
        assert mode & stat.S_IXUSR, f"{wrapper.name} is not executable"


def test_wrappers_are_fail_loud_bash():
    """Each wrapper must be a bash script with strict-mode so a failing inner
    command propagates a non-zero exit (the node's done-gate semantics)."""
    for nid in NODE_IDS:
        text = (P9_DIR / f"{nid}.sh").read_text()
        assert text.startswith("#!/usr/bin/env bash"), nid
        assert "set -euo pipefail" in text, nid


def test_verify_cmd_token_passes_the_add_task_lint():
    """The `make p9-verify-<id>` form still passes the lint (it always did, and
    still does after the relax). The make-indirection harness remains valid even
    though raw compound verify_cmds are now also accepted (see below)."""
    for nid in NODE_IDS:
        cmd = f"make p9-verify-{nid}"
        assert lint_verify_cmd(cmd) is None, f"{cmd!r} rejected: {lint_verify_cmd(cmd)}"


def test_bash_and_compound_forms_now_accepted_by_lint():
    """REWRITTEN pin (2026-06-30, user decision 'full relax'): this previously
    asserted the lint REJECTED `bash …` and raw `&&` verify_cmds — the reason the
    p9 make-indirection harness exists. The user chose to allow shell operators,
    so those forms now PASS the lint. The make harness still works (above); it is
    no longer the ONLY loadable form."""
    assert lint_verify_cmd("bash scripts/p9_verify/r1.sh") is None
    assert lint_verify_cmd("uv run pytest -q && uv run src/juggle_cli.py --help") is None


def test_makefile_has_p9_pattern_rule():
    mk = (REPO / "Makefile").read_text()
    assert "p9-verify-%:" in mk
    assert "bash scripts/p9_verify/$*.sh" in mk
