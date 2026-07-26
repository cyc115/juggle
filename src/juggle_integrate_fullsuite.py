"""Integrate full-suite guard (speedup-tier B2, 2026-06-21).

The integrate gate runs its configured ``test_cmd`` VERBATIM (the 2026-06-20
always-full-suite directive). The speedup-tier ``slow`` marker tiers ONLY the
opt-in developer inner loop (``make test-fast``); it must NEVER deselect at
integrate. This guard inspects a pytest ``test_cmd`` and reports the ways it
would silently SUBSET the suite, so integrate can FAIL LOUD before running it.

A loud refusal is NOT the command-munging the 2026-06-20 directive removed:
munging silently rewrites the command; this leaves ``test_cmd`` untouched and
surfaces the problem. Owns ONLY this string inspection — not the integrate
pipeline (juggle_cmd_integrate) and not the env contract (juggle_integrate_env).
"""
from __future__ import annotations

import re
import subprocess

from juggle_integrate_env import dropped_overrides, format_env_report, sanitized_env

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


def _run_once(
    test_cmd: str, worktree_path: str, env: dict[str, str] | None
) -> subprocess.CompletedProcess:
    """The ONE place integrate launches ``test_cmd`` (call + retry share it).

    ``test_cmd`` is run VERBATIM under ``shell=True`` — the 2026-06-20
    no-munging directive. Only the ENVIRONMENT is integrate's to control.
    """
    return subprocess.run(
        test_cmd, shell=True, capture_output=True, text=True, cwd=worktree_path, env=env
    )


# pytest's default `short test summary info` block lines (reportchars 'fE').
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")

#: Node ids shown per run in a refusal — enough to act on, short enough to read.
_MAX_NODES_SHOWN = 20


def failing_node_ids(stdout: str) -> list[str]:
    """Sorted, de-duplicated test node ids from a pytest run's short summary.

    Empty for a non-pytest ``test_cmd`` or output without a summary block —
    the caller degrades to UNDETERMINED rather than guessing.
    """
    found = set()
    for line in stdout.splitlines():
        m = _SUMMARY_LINE.match(line.strip())
        if m:
            found.add(m.group(1))
    return sorted(found)


def _show(nodes: list[str]) -> str:
    shown = ", ".join(nodes[:_MAX_NODES_SHOWN])
    extra = len(nodes) - _MAX_NODES_SHOWN
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def retry_verdict(first_stdout: str, second_stdout: str) -> str:
    """Was the doubled suite run a flake check, or a confirmation of a real red?

    The retry exists for transient flakes, but against a DETERMINISTIC failure
    it merely doubles an ~8-minute wait while implying a flake was ruled out
    (2026-07-25 cyc_LI incident). Comparing the two runs' failing sets turns
    that wasted wall-clock into the answer the operator actually needs.
    """
    first, second = failing_node_ids(first_stdout), failing_node_ids(second_stdout)
    if not first or not second:
        return (
            "retry: UNDETERMINED — no pytest failure summary could be parsed from "
            "the output, so running twice ruled nothing out."
        )
    if first == second:
        return (
            f"retry: DETERMINISTIC — the SAME {len(first)} test(s) failed on BOTH "
            f"runs, so the retry did NOT rule out a flake, it CONFIRMED a real "
            f"red: {_show(first)}"
        )
    return (
        "retry: FLAKY-LOOKING — the two runs failed DIFFERENT tests. "
        f"run 1: {_show(first)} | run 2: {_show(second)}"
    )


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
    env = sanitized_env()
    dropped = dropped_overrides()
    result = _run_once(test_cmd, worktree_path, env)
    if result.returncode != 0:
        # One retry — for transient flakes, AND to discriminate them from a
        # deterministic red (retry_verdict compares the two failing sets).
        first_stdout = result.stdout
        result = _run_once(test_cmd, worktree_path, env)
        if result.returncode != 0:
            return False, (
                f"Tests failed (exit {result.returncode}) for {worktree_branch}. "
                f"No merge performed.\n"
                f"{format_env_report(dropped)}\n"
                f"{retry_verdict(first_stdout, result.stdout)}\n"
                f"stdout tail: {result.stdout[-300:].strip()}"
            )
    return True, ""
