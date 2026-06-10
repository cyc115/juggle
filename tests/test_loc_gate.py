"""Tests for the LOC gate (scripts/loc_gate.py).

Phase 0 of the 2026-06-10 refactor plan: every git-tracked Python module in
src/ (and Python scripts in scripts/) must be <=300 lines unless grandfathered
in the gate's allowlist at its current line count. The allowlist may only
shrink: entries must currently exceed the limit (stale entries fail here,
forcing removal) and budgets may only be lowered.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
GATE = REPO_ROOT / "scripts" / "loc_gate.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _run_gate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


# ── subprocess integration ─────────────────────────────────────────────────────


def test_gate_script_exists():
    assert GATE.exists(), "scripts/loc_gate.py missing"


def test_gate_passes_on_current_tree():
    """All current offenders are grandfathered, so the gate must exit 0."""
    proc = _run_gate()
    assert proc.returncode == 0, f"loc gate failed:\n{proc.stdout}\n{proc.stderr}"


def test_gate_json_mode():
    proc = _run_gate("--json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["limit"] == 300
    assert data["offenders"] == []
    assert isinstance(data["allowlist"], dict)
    assert data["files_checked"] > 0


def test_gate_update_baseline_prints_only():
    """--update-baseline prints a fresh allowlist but never writes the script."""
    before = GATE.read_bytes()
    proc = _run_gate("--update-baseline")
    assert proc.returncode == 0
    assert "GRANDFATHERED" in proc.stdout
    assert GATE.read_bytes() == before, "--update-baseline must not modify the gate"


# ── allowlist-shrink invariant ─────────────────────────────────────────────────


def test_allowlist_entries_all_currently_exceed_limit():
    """Every grandfathered entry must still exceed the 300-line limit.

    A stale entry (file shrunk to <=300, or file deleted) fails this test,
    forcing its removal — the allowlist may only shrink.
    """
    import loc_gate

    for path, budget in loc_gate.GRANDFATHERED.items():
        f = REPO_ROOT / path
        assert f.exists(), f"stale allowlist entry (file gone): {path}"
        n = loc_gate.count_lines(f)
        assert n > loc_gate.LIMIT, (
            f"stale allowlist entry {path}: now {n} lines (<= {loc_gate.LIMIT}) "
            "— remove it from GRANDFATHERED"
        )
        assert budget > loc_gate.LIMIT, (
            f"allowlist budget for {path} is {budget} (<= limit) — remove the entry"
        )


# ── pure evaluation logic ──────────────────────────────────────────────────────


def test_evaluate_flags_new_offender():
    import loc_gate

    result = loc_gate.evaluate({"src/new_big.py": 301}, {}, limit=300)
    assert [o["path"] for o in result["offenders"]] == ["src/new_big.py"]


def test_evaluate_allows_grandfathered_within_budget():
    import loc_gate

    result = loc_gate.evaluate(
        {"src/big.py": 500}, {"src/big.py": 500}, limit=300
    )
    assert result["offenders"] == []


def test_evaluate_flags_grandfathered_file_that_grew():
    """A grandfathered file growing past its recorded budget fails the gate."""
    import loc_gate

    result = loc_gate.evaluate(
        {"src/big.py": 501}, {"src/big.py": 500}, limit=300
    )
    assert [o["path"] for o in result["offenders"]] == ["src/big.py"]


def test_evaluate_reports_stale_entries():
    import loc_gate

    result = loc_gate.evaluate(
        {"src/shrunk.py": 120}, {"src/shrunk.py": 500}, limit=300
    )
    assert result["offenders"] == []
    assert result["stale"] == ["src/shrunk.py"]


def test_evaluate_under_limit_passes():
    import loc_gate

    result = loc_gate.evaluate({"src/small.py": 299}, {}, limit=300)
    assert result["offenders"] == []
    assert result["stale"] == []
