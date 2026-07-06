"""tests/test_vcs_recipe_toy.py — the bundled TOY recipe run against the SHARED
vcs_conformance kit, proving the agentic/recipe path can reach conformance-green
with NO external VCS on the machine (the design's toy-recipe acceptance gate).

The toy backend is a real (tiny) snapshot-DAG VCS implemented in pure Python —
it depends on no git/sl binary — so this is genuine behavioral conformance, not
a scripted double: real workspace fork, real commit/ancestry, a real
rebase-equivalent (incl. a genuine conflict AND interrupted-update recovery),
and a full submit(direct) -> land_status lifecycle.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_vcs_harness import ToyHarness  # noqa: E402

# Re-export every test_* function from the shared kit so pytest collects them
# HERE, resolving `conformance_harness` against the fixture below.
from vcs_conformance import *  # noqa: F401,F403,E402


@pytest.fixture
def conformance_harness(tmp_path):
    return ToyHarness(tmp_path)
