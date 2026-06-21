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
