"""N4 slimming pass: deprecated flat schedule shims are retired.

The four `src/juggle_schedule_{common,autofix,reflect,dogfood}.py` shims
(`from schedules.X import *` re-exports, added 2026-06-10 Phase 3) have no
remaining real importers — verified by grep before deletion — so they are
deleted outright rather than kept for compatibility.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))


def test_flat_shim_files_removed():
    for name in ("common", "autofix", "reflect", "dogfood"):
        assert not (SRC / f"juggle_schedule_{name}.py").exists()


def test_flat_shim_modules_not_importable():
    import importlib

    for name in ("common", "autofix", "reflect", "dogfood"):
        try:
            importlib.import_module(f"juggle_schedule_{name}")
            assert False, f"juggle_schedule_{name} should no longer be importable"
        except ModuleNotFoundError:
            pass


def test_llm_calls_docstring_points_to_schedules_common():
    text = (SRC / "llm_calls.py").read_text()
    assert "schedules.common.claude_p" in text
    assert "juggle_schedule_common.claude_p" not in text
