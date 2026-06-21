"""Integrate full-suite guard (speedup-tier B2, 2026-06-21).

The integrate gate runs its configured ``test_cmd`` VERBATIM (the 2026-06-20
always-full-suite directive). The speedup-tier ``slow`` marker tiers ONLY the
opt-in developer inner loop (``make test-fast``); it must NEVER deselect at
integrate. This guard inspects a pytest ``test_cmd`` and reports the ways it
would silently SUBSET the suite, so integrate can FAIL LOUD before running it.

A loud refusal is NOT the command-munging the 2026-06-20 directive removed:
munging silently rewrites the command; this leaves ``test_cmd`` untouched and
surfaces the problem. Owns ONLY this string inspection — not the integrate
pipeline (juggle_cmd_integrate) nor verify (juggle_integrate_verify).
"""
from __future__ import annotations

import subprocess

# Substrings in a pytest ``test_cmd`` that would subset the FULL suite. Note that
# ``not watchdog_proc`` is intentionally NOT here: those destructive proc-spawning
# tests are opt-in by design (2026-06-16 incident), not the slow speedup tier.
_SUBSET_SIGNS: tuple[tuple[str, str], ...] = (
    ("not slow", "deselects the speedup-tier `slow` marker (fast-tier only)"),
    ("--deselect", "deselects specific tests"),
    ("--ignore", "ignores test paths"),
)


def full_suite_violations(test_cmd: str) -> list[str]:
    """Reasons ``test_cmd`` would NOT run the full suite (empty list = OK).

    Only pytest invocations are inspected; a non-pytest ``test_cmd`` (``make
    ci``, a wrapper script, ...) is the operator's business and returns ``[]``.
    """
    cmd = (test_cmd or "").strip()
    if not cmd or "pytest" not in cmd:
        return []
    return [f"`{sign}` — {why}" for sign, why in _SUBSET_SIGNS if sign in cmd]


def run_test_cmd_full(
    test_cmd: str, worktree_path: str, worktree_branch: str
) -> tuple[bool, str]:
    """Run the integrate ``test_cmd`` as the FULL suite (one retry on flake).

    Returns ``(ok, fail_reason)``. FAILS LOUD before running if ``test_cmd``
    would SUBSET the suite (B2) — a refusal, NOT munging: the command is left
    verbatim, integrate just aborts instead of running a quiet subset.
    """
    viol = full_suite_violations(test_cmd)
    if viol:
        return False, (
            f"Configured test_cmd would NOT run the FULL suite for "
            f"{worktree_branch} (always-full-suite directive, B2): "
            + "; ".join(viol)
            + ". The `slow` marker tiers only the opt-in `make test-fast` inner "
            "loop — never integrate. Set test_cmd to the full suite (e.g. "
            "`uv run pytest -n auto --dist loadgroup -m 'not watchdog_proc'`)."
        )
    result = subprocess.run(
        test_cmd, shell=True, capture_output=True, text=True, cwd=worktree_path
    )
    if result.returncode != 0:
        # One retry for transient flakes (pilot/Textual tests flake under load).
        result = subprocess.run(
            test_cmd, shell=True, capture_output=True, text=True, cwd=worktree_path
        )
    if result.returncode != 0:
        return False, (
            f"Tests failed (exit {result.returncode}) for {worktree_branch}. "
            f"No merge performed. stdout tail: {result.stdout[-300:].strip()}"
        )
    return True, ""
