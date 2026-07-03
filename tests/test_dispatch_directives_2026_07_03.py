"""T-fix-dispatch-plan-spec-provision — two user-directed dispatch-contract
changes, both authored in the template SOURCE (juggle_cmd_agents_common /
juggle_task_templates), not copied elsewhere:

1. Pre-existing test failures: coders must proactively FIX them (not wave them
   through) — the old "NOT your concern" universal rule regressed intent.
2. Test re-run limit: coders may re-run/re-fix as many times as needed until
   green or a genuine blocker — the old "ONE fix attempt, STOP if still red"
   verification text regressed intent.
"""
from __future__ import annotations

from juggle_cmd_agents_common import UNIVERSAL_PREAMBLE
from juggle_task_templates import TASK_TEMPLATES


def test_universal_preamble_no_longer_waves_through_pre_existing_failures():
    assert "not your concern" not in UNIVERSAL_PREAMBLE.lower()


def test_universal_preamble_instructs_fixing_pre_existing_failures():
    text = UNIVERSAL_PREAMBLE.lower()
    assert "fix" in text
    assert "pre-existing" in text
    assert "retain" in text  # still noted in --retain per the directive


def test_coder_verification_no_longer_caps_fix_attempts_at_one():
    coder = TASK_TEMPLATES["coder"].lower()
    assert "one fix attempt" not in coder
    assert "one green run = done" not in coder


def test_coder_verification_allows_iterating_until_green_or_blocker():
    coder = TASK_TEMPLATES["coder"]
    low = coder.lower()
    assert "until green" in low or "as many times as needed" in low
    assert "blocker" in low
    assert "iteration count" in low or "report" in low
