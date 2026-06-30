"""P9 verify-harness smoke test.

Pins the harness that lets the 18-node P9 CLI-grammar-migration DAG load:
every node's `verify_cmd` is the single operator-free token `make p9-verify-<id>`,
backed by an executable wrapper in `scripts/p9_verify/<id>.sh` and the
`p9-verify-%` Makefile pattern rule.

Incident this guards (2026-06-29): `juggle graph add-task --verify-cmd` lint
FORBIDS shell operators and only allowlists {make,uv,pytest,python,...} as the
exe — `bash` is NOT allowlisted — so the §6 compound verify_cmds cannot load
verbatim. The `make p9-verify-<id>` indirection is the fix.
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
    """The core invariant: `make p9-verify-<id>` is the form that survives the
    lint which rejected the raw `&&` verify_cmds. If this regresses, the DAG
    cannot load."""
    for nid in NODE_IDS:
        cmd = f"make p9-verify-{nid}"
        assert lint_verify_cmd(cmd) is None, f"{cmd!r} rejected: {lint_verify_cmd(cmd)}"


def test_bash_form_is_rejected_by_lint():
    """Documents WHY the make-indirection exists: a direct `bash …` verify_cmd
    (and the raw compound form) fail the lint."""
    assert lint_verify_cmd("bash scripts/p9_verify/r1.sh") is not None
    assert lint_verify_cmd("uv run pytest -q && uv run src/juggle_cli.py --help") is not None


def test_makefile_has_p9_pattern_rule():
    mk = (REPO / "Makefile").read_text()
    assert "p9-verify-%:" in mk
    assert "bash scripts/p9_verify/$*.sh" in mk
