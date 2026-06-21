"""Fast/slow tier pins (speedup-tier B2, 2026-06-21).

The bare `pytest` inner loop is the FULL suite — `not slow` is DELIBERATELY
ABSENT from global addopts — so the always-full-suite integrate gate (which runs
its `test_cmd` VERBATIM) cannot be silently downgraded to fast-tier-only. The
fast tier is OPT-IN (`make test-fast` / `-m 'not slow and not watchdog_proc'`).

These are REAL regression pins (NOT the plan's original tautological literal
assertion): they read the EFFECTIVE config and the integrate guard, and FAIL if
someone re-introduces global slow-deselection or weakens the full-suite guard.
"""
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def test_global_addopts_does_not_deselect_slow():
    """B2 regression pin: bare `pytest` MUST stay the FULL suite. Re-introducing
    `not slow` into global addopts is the directive-violating flaw — a verbatim
    `test_cmd = "uv run pytest"` integrate gate would silently run fast-tier-only.
    (RED on the plan's original Task 5 Step 2, which set this clause.)"""
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
    assert "not slow" not in addopts, (
        f"global addopts must NOT deselect slow (B2); got: {addopts!r}"
    )


def test_integrate_fullsuite_guard_rejects_subsetting_and_allows_full():
    """B2 regression pin: the integrate full-suite guard FAILS LOUD on a test_cmd
    that would subset the suite (`not slow`, `--deselect`, `--ignore`) and passes
    the documented full-parallel form / non-pytest cmds."""
    sys.path.insert(0, str(_ROOT / "src"))
    from juggle_integrate_fullsuite import full_suite_violations

    # Subsetting pytest test_cmds → flagged (non-empty violation list).
    assert full_suite_violations("uv run pytest -m 'not slow'")
    assert full_suite_violations("uv run pytest -m 'not watchdog_proc and not slow'")
    assert full_suite_violations("uv run pytest --deselect tests/test_x.py::t")
    assert full_suite_violations("uv run pytest --ignore=tests/slow")
    # Full-suite forms → no violation.
    assert full_suite_violations("") == []
    assert full_suite_violations("uv run pytest -q") == []
    assert full_suite_violations(
        "uv run pytest -n auto --dist loadgroup -m 'not watchdog_proc'"
    ) == []
    # A non-pytest test_cmd is the operator's business, not subsetting.
    assert full_suite_violations("make ci") == []


def test_integrate_invokes_fullsuite_guard_and_runs_verbatim():
    """B2 regression pin: integrate delegates the suite run to the guarded runner,
    which checks for subsetting BEFORE running and still runs test_cmd VERBATIM
    (no command-munging — the 2026-06-20 directive). A loud refusal is not munging;
    the command is intact."""
    integrate_src = (_ROOT / "src" / "juggle_cmd_integrate.py").read_text()
    fullsuite_src = (_ROOT / "src" / "juggle_integrate_fullsuite.py").read_text()
    assert "run_test_cmd_full" in integrate_src, (
        "integrate must delegate the test_cmd run to the B2-guarded runner"
    )
    assert "full_suite_violations" in fullsuite_src, (
        "the runner must check for subsetting before running test_cmd"
    )
    assert "test_cmd, shell=True" in fullsuite_src, (
        "the runner must still run test_cmd verbatim (no munging)"
    )


def _collect_count(marker_expr: str) -> int:
    """SELECTED test count for a marker expr. Parses the summary line
    `N/M tests collected (D deselected)` (or `N tests collected`) → N (selected,
    the first number — NOT the total M)."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-m", marker_expr],
        capture_output=True, text=True, cwd=str(_ROOT),
    )
    m = re.search(r"(\d+)(?:/\d+)? tests? collected", r.stdout)
    return int(m.group(1)) if m else 0


@pytest.mark.slow
def test_fast_opt_in_tier_is_strict_subset_of_full():
    """B2 regression pin: the OPT-IN fast tier (`not slow and not watchdog_proc`)
    collects strictly FEWER than the default full suite (`not watchdog_proc`) —
    proving `slow` actually tiers heavy buckets — while neither is empty."""
    fast = _collect_count("not slow and not watchdog_proc")
    full = _collect_count("not watchdog_proc")
    assert 0 < fast < full, f"fast={fast} full={full}: slow marker not tiering"
